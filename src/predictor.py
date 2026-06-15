"""
predictor.py
============
Módulo de inferencia espacial de tráfico mediante IDW (Inverse Distance Weighting).

Posición en el pipeline:
    ETL (TomTom → fact_lecturas_trafico)  →  predictor.py  →  trafico_inferido  →  Grafana

Responsabilidad:
    Tomar las lecturas más recientes de las avenidas medidas y generar una
    superficie continua de congestión sobre una grilla que cubre la zona
    metropolitana de San Luis Potosí. Cada punto de la grilla recibe un nivel
    de congestión inferido por distancia ponderada inversa (IDW).

Dependencias:
    pip install geopandas shapely scipy numpy sqlalchemy psycopg2-binary pandas
"""

import logging
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from shapely.geometry import Point, box
from sqlalchemy import create_engine, text
import config

# ---------------------------------------------------------------------------
# Configuración de logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parámetros ajustables — modifícalos sin tocar la lógica
# ---------------------------------------------------------------------------

# Cadena de conexión a PostgreSQL. Ajusta usuario/contraseña/host según tu
# docker-compose o variables de entorno.
DB_URL = config.DB_URL

# Bounding box de la zona metropolitana de SLP en WGS84 (lon_min, lat_min, lon_max, lat_max).
# Estos valores cubren la mancha urbana principal. Amplíalos si tu red de avenidas
# cubre zonas periféricas como Soledad de Graciano Sánchez.
#BBOX = (-101.05, 22.08, -100.89, 22.21)
BBOX = (-101.03, 22.11, -100.96, 22.19)

# Resolución de la grilla: distancia entre puntos en METROS.
# 200m → grilla densa (~2500 puntos sobre SLP), buena para heatmap detallado.
# 500m → grilla ligera (~400 puntos), útil para pruebas rápidas.
GRID_RESOLUTION_M = 200

# Número de vecinos medidos que participan en cada cálculo IDW.
# Con K=5 el resultado es robusto sin ser dominado por un solo segmento atípico.
# Aumentar K suaviza el mapa; reducirlo lo hace más "pico".
K_VECINOS = 5

# Exponente de distancia del IDW.
# p=2 es el estándar (peso ∝ 1/d²). Valores mayores concentran más la influencia
# en el vecino más cercano; valores menores distribuyen más uniformemente.
POTENCIA_IDW = 2

# Radio máximo de influencia en metros.
# Un punto de grilla cuyo vecino fuente más cercano esté más lejos que este valor
# recibe congestion = 1.0 (flujo libre neutro) en lugar de un valor interpolado.
# Esto evita que avenidas lejanas "tiñan" zonas de la ciudad donde no hay medición.
# 600m cubre bien las manzanas entre avenidas principales sin generar manchas.
# Ajusta hacia arriba si ves zonas oscuras donde SÍ hay avenidas cubiertas.
DISTANCIA_MAX_M = 600

# SRID métrico para SLP. UTM Zona 14N cubre toda la región y mide en metros,
# lo que permite calcular distancias reales sin convertir grados a metros a mano.
SRID_METRICO = 32614


# ---------------------------------------------------------------------------
# Etapa 1 — Leer el estado actual de la base de datos
# ---------------------------------------------------------------------------

def leer_lecturas_recientes(engine) -> gpd.GeoDataFrame:
    """
    Trae la lectura más reciente de cada segmento vial medido.

    Usamos un subquery con ROW_NUMBER() para quedarnos con una sola fila
    por segmento (la más reciente), evitando duplicados que distorsionarían
    el IDW. El JOIN con dim_segmentos nos da la geometría en una sola consulta.

    Retorna un GeoDataFrame con columnas:
        id_segmento, nombre_via, nivel_congestion, geom (LineString WGS84)
    """
    log.info("Etapa 1: leyendo lecturas recientes de la base de datos...")

    sql = ("""
        WITH ultima_lectura AS (
            SELECT
                id_segmento,
                nivel_congestion,
                ROW_NUMBER() OVER (
                    PARTITION BY id_segmento
                    ORDER BY timestamp_lectura DESC
                ) AS rn
            FROM fact_lecturas_trafico
        )
        SELECT
            d.id_segmento,
            d.nombre_via,
            u.nivel_congestion,
            d.geom
        FROM dim_segmentos_viales d
        JOIN ultima_lectura u
            ON d.id_segmento = u.id_segmento
           AND u.rn = 1
        WHERE d.geom IS NOT NULL
    """)

    try:
        with engine.connect() as conn:
            conexion_nativa = conn.connection
            gdf = gpd.read_postgis(sql, con=conexion_nativa, geom_col='geom')
        log.info(f" -> {len(gdf)} segmentos medidos cargados.")
        return gdf
    except Exception as e:
        log.error(f"Error fatal extrayendo geometrias de PostGIS: {e}")
        raise


# ---------------------------------------------------------------------------
# Etapa 2 — Muestrear puntos equidistantes sobre cada segmento
# ---------------------------------------------------------------------------

# Distancia en metros entre puntos de muestreo sobre cada LineString.
# 150m significa que un segmento de 600m genera 4 puntos fuente en lugar de 1.
# Valores menores = cobertura más densa = heatmap más continuo.
MUESTREO_LINEA_M = 75

def muestrear_puntos_linea(gdf_fuentes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Convierte cada LineString en N puntos equidistantes, todos con el mismo
    nivel_congestion del segmento padre.

    Por qué esto resuelve los vacíos del heatmap:
    Con un solo centroide por segmento, la influencia IDW decae rápidamente
    hacia los extremos de la avenida. Una calle de 1.5km representada como
    un punto en su km 0.75 aporta muy poco calor a los puntos de grilla
    cerca de su inicio o su final.

    Al muestrear puntos cada MUESTREO_LINEA_M metros, la influencia se
    distribuye uniformemente a lo largo de toda la longitud de la vía,
    produciendo un heatmap continuo sin vacíos entre avenidas.

    shapely.interpolate(distancia) genera un Point en la posición exacta
    a `distancia` metros siguiendo los vértices reales de la línea.

    Retorna un GeoDataFrame de Points en EPSG:32614 (metros UTM 14N),
    con más filas que el input (una por cada punto de muestreo).
    """
    log.info(
        f"Etapa 2: muestreando puntos cada {MUESTREO_LINEA_M}m sobre cada segmento..."
    )

    # Proyectar a metros primero — interpolate() usa las unidades del CRS.
    # En WGS84 (grados) interpolate(150) generaría un punto a 150 GRADOS,
    # que está fuera del planeta. En UTM 14N genera un punto a 150 metros.
    gdf_m = gdf_fuentes.to_crs(epsg=SRID_METRICO)

    registros = []
    for _, fila in gdf_m.iterrows():
        linea    = fila["geom"]
        longitud = linea.length  # metros (CRS métrico)

        if longitud == 0:
            # Segmento degenerado (punto único), usar su coordenada directamente
            registros.append({
                "id_segmento":      fila.id_segmento,
                "nombre_via":       fila.nombre_via,
                "nivel_congestion": fila.nivel_congestion,
                "geom":         fila["geom"].centroid,
            })
            continue

        # Posiciones de muestreo: 0m, 150m, 300m, ... + el punto final exacto.
        distancias = list(np.arange(0, longitud, MUESTREO_LINEA_M)) + [longitud]

        for d in distancias:
            registros.append({
                "id_segmento":      fila.id_segmento,
                "nombre_via":       fila.nombre_via,
                "nivel_congestion": fila.nivel_congestion,
                "geom":         linea.interpolate(d),
            })

    gdf_puntos = gpd.GeoDataFrame(registros, geometry="geom", crs=f"EPSG:{SRID_METRICO}")

    log.info(
        f"  → {len(gdf_fuentes)} segmentos → {len(gdf_puntos)} puntos fuente "
        f"(~{len(gdf_puntos)/len(gdf_fuentes):.1f} puntos/segmento)"
    )
    return gdf_puntos


# ---------------------------------------------------------------------------
# Etapa 3 — Generar la grilla de puntos objetivo
# ---------------------------------------------------------------------------

def generar_grilla(bbox_wgs84: tuple, resolucion_m: int) -> gpd.GeoDataFrame:
    """
    Crea una malla regular de puntos sobre la zona metropolitana.

    El proceso es:
    1. Crear un polígono bounding box en WGS84.
    2. Proyectarlo a UTM 14N para poder aplicar la resolución en metros.
    3. Generar puntos en una cuadrícula con paso = resolucion_m.
    4. Guardar tanto la geometría métrica (para el IDW) como las coordenadas
       WGS84 originales (para insertar en la DB y visualizar en Grafana).

    Los puntos de la grilla son el "lienzo en blanco" sobre el que el IDW
    va a pintar los valores de congestión inferidos.
    """
    log.info(f"Etapa 3: generando grilla con resolución {resolucion_m}m...")

    # Polígono bbox en WGS84
    bbox_poly = gpd.GeoDataFrame(
        geometry=[box(*bbox_wgs84)],
        crs="EPSG:4326"
    )

    # Proyectar a metros para que np.arange trabaje en metros reales
    bbox_m = bbox_poly.to_crs(epsg=SRID_METRICO)
    minx, miny, maxx, maxy = bbox_m.total_bounds

    # Crear coordenadas de la malla
    xs = np.arange(minx, maxx, resolucion_m)
    ys = np.arange(miny, maxy, resolucion_m)
    xx, yy = np.meshgrid(xs, ys)
    coords_m = np.column_stack([xx.ravel(), yy.ravel()])

    # GeoDataFrame métrico (para el cálculo IDW)
    gdf_grilla = gpd.GeoDataFrame(
        geometry=[Point(x, y) for x, y in coords_m],
        crs=f"EPSG:{SRID_METRICO}"
    )

    # Guardar las coordenadas WGS84 para la inserción en DB
    gdf_grilla_wgs = gdf_grilla.to_crs(epsg=4326)
    gdf_grilla["lat"] = gdf_grilla_wgs.geometry.y
    gdf_grilla["lon"] = gdf_grilla_wgs.geometry.x

    log.info(f"  → {len(gdf_grilla)} puntos de grilla generados.")
    return gdf_grilla


# ---------------------------------------------------------------------------
# Etapa 4 — Aplicar IDW (el corazón del script)
# ---------------------------------------------------------------------------

def aplicar_idw(
    gdf_grilla: gpd.GeoDataFrame,
    gdf_fuentes: gpd.GeoDataFrame,
    k: int,
    potencia: float,
    distancia_max: float,
) -> np.ndarray:
    """
    Calcula el nivel de congestión inferido en cada punto de la grilla
    usando Inverse Distance Weighting.

    Algoritmo paso a paso:
    ─────────────────────
    1. Extraer coordenadas (x, y) en metros de fuentes y puntos objetivo.
    2. Construir un cKDTree sobre las fuentes. Un KDTree es una estructura
       de datos en árbol que permite encontrar los K vecinos más cercanos en
       O(log N) en vez de O(N). Con 2500 puntos de grilla y 20 fuentes no
       hay diferencia, pero el hábito es correcto para escalar.
    3. Para cada punto de la grilla, pedir los K vecinos más cercanos y sus
       distancias. Retorna dos arrays: distances[n_puntos, K] e indices[n_puntos, K].
    4. Calcular pesos: w_i = 1 / dist_i^p. Normalizar para que sumen 1.
    5. Calcular congestión inferida: suma ponderada de los niveles de las fuentes.

    Caso borde: si un punto de la grilla cae exactamente encima de una fuente
    (distancia = 0), su congestión inferida es la de esa fuente directamente,
    sin división por cero. Esto se maneja con np.where.

    Retorna un array de forma (n_puntos_grilla,) con valores en [0, 1].
    """
    log.info(f"Etapa 4: aplicando IDW (K={k}, p={potencia})...")

    # Coordenadas como arrays numpy: forma (n, 2)
    coords_fuentes = np.array(
        [geom.coords[0] for geom in gdf_fuentes.geometry]
    )
    coords_objetivo = np.array(
        [geom.coords[0] for geom in gdf_grilla.geometry]
    )
    valores_fuentes = gdf_fuentes["nivel_congestion"].values.astype(float)

    # Árbol espacial sobre las fuentes
    tree = cKDTree(coords_fuentes)

    # Consulta: K vecinos más cercanos para cada punto objetivo
    # distances: (n_objetivo, K), indices: (n_objetivo, K)
    distancias, indices = tree.query(coords_objetivo, k=min(k, len(gdf_fuentes)))

    # Manejo de distancia cero (punto exactamente sobre una fuente)
    # En ese caso el peso de esa fuente es infinito → se toma su valor directo.
    distancias = np.where(distancias == 0, 1e-10, distancias)

    # Calcular pesos IDW: w = 1 / d^p
    pesos = 1.0 / np.power(distancias, potencia)          # (n_objetivo, K)
    pesos_norm = pesos / pesos.sum(axis=1, keepdims=True)  # normalizar a suma=1

    # Recoger los valores de congestión de los vecinos seleccionados
    # indices tiene forma (n_objetivo, K); valores_fuentes[indices] recoge
    # los niveles de congestión de esos K vecinos para cada punto objetivo.
    congestion_vecinos = valores_fuentes[indices]          # (n_objetivo, K)

    # Suma ponderada final
    congestion_inferida = np.sum(pesos_norm * congestion_vecinos, axis=1)

    # mascara de distancia maxima
    # distancias [:,0] es la distancia al vecino mas cercano de cada punto
    # si ese vecino ya esta mas lejos de lo permitido por DISTANCIA_MAX_M, el punto esta fuera
    # de cobertura y asignamos 1.0 (un flujo libre) en lugar de propagar 
    # influencias de avenidas lejanas, causando quitar las manchas difusas
    fuera_de_rango = distancias[:, 0] > distancia_max
    congestion_inferida[fuera_de_rango] = 1.0
    log.info(
        f"  → {fuera_de_rango.sum()} puntos fuera de rango ({distancia_max}m) "
        f"asignados a 1.0 ({fuera_de_rango.mean()*100:.1f}% de la grilla)"
    )

    # Asegurar que el resultado esté en [0, 1] (puede salirse por error numérico)
    congestion_inferida = np.clip(congestion_inferida, 1.0, 3.0)

    log.info(
        f"  → Congestión inferida: "
        f"min={congestion_inferida.min():.3f}, "
        f"media={congestion_inferida.mean():.3f}, "
        f"max={congestion_inferida.max():.3f}"
    )
    return congestion_inferida


# ---------------------------------------------------------------------------
# Etapa 5 — Escribir resultados en trafico_inferido
# ---------------------------------------------------------------------------

def guardar_resultados(
    engine,
    gdf_grilla: gpd.GeoDataFrame,
    congestion_inferida: np.ndarray,
    timestamp_calculo: datetime,
) -> None:
    """
    Inserta los puntos inferidos en la tabla trafico_inferido.

    Por qué INSERT en lote y no fila a fila: un único executemany() con
    todos los puntos hace una sola transacción de red hacia PostgreSQL.
    Insertar fila a fila con 2500 puntos haría 2500 round-trips de red.

    La columna segmento_origen_id se deja NULL para los puntos de grilla,
    porque no corresponden a un segmento específico. Cuando en fases futuras
    infieras sobre calles secundarias reales, ahí sí tendrá valor.
    """
    log.info("Etapa 5: guardando resultados en trafico_inferido...")

    registros = [
        {
            "timestamp_calculo":    timestamp_calculo,
            "segmento_origen_id":   None,          # punto de grilla, sin segmento fijo
            "nivel_congestion_inf": float(c),
            "distancia_metros":     None,           # sin vecino único referenciado
            "lat":                  float(row.lat),
            "lon":                  float(row.lon),
            "metodo_inferencia":    "IDW_GRID",
        }
        for (_, row), c in zip(gdf_grilla.iterrows(), congestion_inferida)
    ]

    insert_sql = text("""
        INSERT INTO trafico_inferido
            (timestamp_calculo, segmento_origen_id,
             nivel_congestion_inf, distancia_metros,
             lat, lon, metodo_inferencia)
        VALUES
            (:timestamp_calculo, :segmento_origen_id,
             :nivel_congestion_inf, :distancia_metros,
             :lat, :lon, :metodo_inferencia)
    """)

    with engine.begin() as conn:
        conn.execute(insert_sql, registros)

    log.info(f"  → {len(registros)} puntos insertados correctamente.")


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------

def ejecutar(engine):
    """
    Punto de entrada del script. Orquesta las cinco etapas en orden.
    Puede llamarse directamente o importarse desde el ETL principal.
    """
    log.info("=== predictor.py iniciando ===")
    timestamp = datetime.now(tz=timezone.utc)

    #engine = create_engine(DB_URL, pool_pre_ping=True)

    # Etapa 1
    gdf_fuentes_geo = leer_lecturas_recientes(engine)

    # Etapa 2
    gdf_fuentes_m = muestrear_puntos_linea(gdf_fuentes_geo)

    # Etapa 3
    gdf_grilla = generar_grilla(BBOX, GRID_RESOLUTION_M)

    # Etapa 4
    congestion = aplicar_idw(gdf_grilla, gdf_fuentes_m, K_VECINOS, POTENCIA_IDW, DISTANCIA_MAX_M)

    # Etapa 5
    guardar_resultados(engine, gdf_grilla, congestion, timestamp)

    log.info("=== predictor.py finalizado ===")


if __name__ == "__main__":
    from sqlalchemy import create_engine
    motor_prueba = create_engine(DB_URL)
    ejecutar(motor_prueba)
