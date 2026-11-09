"""
predictor.py
============
Estado del tráfico en TODA la red vial OSM → trafico_calles (mapa de calor y
costo de ruteo).

    versión 3 (actual, D13) — fuente: vector tiles
        fact_flujo_aristas (última pasada, < VENTANA_TILES)
          → aristas medidas: congestión = 1 / traffic_level   (metodo 'TILE')
          → resto: IDW desde las aristas medidas abiertas     (metodo 'IDW_TILE')
    versión 2 (respaldo si no hay pasada reciente) — fuente: Flow Segment Data
        fact_lecturas_trafico (< VENTANA_LECTURAS) → IDW     (metodo 'IDW_OSM')

Lo que sigue describe la versión 2, que se conserva como respaldo.

Posición en el pipeline:
    ETL (TomTom → fact_lecturas_trafico)  →  predictor.py  →  trafico_calles  →  Grafana

Responsabilidad:
    Tomar las lecturas más recientes de las avenidas medidas e inferir
    el nivel de congestion sobre la red vila completa de SLP descargada
    de OpenStreetMap en la tabla dim_calles_osm, se recibe un nivel
    de congestión inferido por distancia ponderada inversa (IDW).

    version 2 - cambio de lienzo, antes el IDW pintaba sobre una grilla
    anonima de puntos en el espacio que representaba el trafico inferido.
    Ahora pinta sobre los centroides de cada calle de la red vial del OSM
    trafico_calles, permitiendonos responder a preguntas mas exactas como
    una Calle en especifico X, en lugar de preguntar en base a puntos 
    lat/lon.

Dependencias:
    pip install geopandas shapely scipy numpy sqlalchemy psycopg2-binary pandas
"""

import logging
from datetime import datetime, timedelta, timezone

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import BigInteger, Column, DateTime, Float, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

# La zona de estudio ya no se define aquí: los puntos objetivo son las calles
# de dim_calles_osm, cargadas con la zona única de zona.py (P9).

# Antigüedad máxima de la lectura más reciente para inferir. Igual al
# compress_after de la migración 008: más allá, el chunk destino puede estar
# comprimido (TimescaleDB 2.10 no admite ON CONFLICT ahí) y además un "tráfico
# actual" de hace más de una semana no sirve para el mapa de calor ni el ruteo.
ANTIGUEDAD_MAXIMA = timedelta(days=7)

# Solo se usan como fuentes del IDW las lecturas de las últimas N horas. Con el
# pool de peticiones (presupuesto.py) cada ciclo mide pocos puntos y el resto
# conserva lecturas viejas: mezclarlas pintaría tráfico de otro día como actual.
# 6 h ≈ el tiempo en que la rotación renueva todos los puntos en horas pico con
# ~800 peticiones/día; en horas valle habrá menos fuentes (más calles en 1.0).
VENTANA_LECTURAS = timedelta(hours=6)

# v3: se usa la última pasada de tiles si tiene menos de esto (2 ciclos de 15 min).
VENTANA_TILES = timedelta(minutes=30)

# v3: tope de congestión medida. traffic_level ≤ 0.1 (≤ 10 % de la velocidad
# libre) o vía cerrada → 10. El tope de 3.0 de la v2 subestimaba atascos reales
# (Segment Data llegó a medir 8). Las vías cerradas se distinguen en
# fact_flujo_aristas.cerrada; el ruteo debe excluirlas, no solo encarecerlas.
CONGESTION_MAX = 10.0

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
    Trae la lectura más reciente de cada segmento vial medido dentro de
    VENTANA_LECTURAS (los segmentos sin lectura reciente no participan).

    Usamos un subquery con ROW_NUMBER() para quedarnos con una sola fila
    por segmento (la más reciente), evitando duplicados que distorsionarían
    el IDW. El JOIN con dim_segmentos nos da la geometría en una sola consulta.

    Retorna un GeoDataFrame con columnas:
        id_segmento, nombre_via, nivel_congestion, timestamp_lectura, geom (LineString WGS84)
    """
    log.info("Etapa 1: leyendo lecturas recientes de la base de datos...")

    sql = ("""
        WITH ultima_lectura AS (
            SELECT
                id_segmento,
                nivel_congestion,
                timestamp_lectura,
                ROW_NUMBER() OVER (
                    PARTITION BY id_segmento
                    ORDER BY timestamp_lectura DESC
                ) AS rn
            FROM fact_lecturas_trafico
            WHERE timestamp_lectura >= %(desde)s
        )
        SELECT
            d.id_segmento,
            d.nombre_via,
            u.nivel_congestion,
            u.timestamp_lectura,
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
            gdf = gpd.read_postgis(
                sql, con=conexion_nativa, geom_col='geom',
                params={"desde": _ahora() - VENTANA_LECTURAS},
            )
        log.info(f" -> {len(gdf)} segmentos con lectura en las últimas {VENTANA_LECTURAS} cargados.")
        return gdf
    except Exception as e:
        log.error(f"Error fatal extrayendo geometrias de PostGIS: {e}")
        raise


def referencia_ciclo(gdf_fuentes: gpd.GeoDataFrame) -> datetime:
    """
    Identificador del estado del tráfico que se va a inferir: la lectura TomTom
    más reciente entre las fuentes. Se guarda como timestamp_calculo, de modo que
    inferir dos veces sobre los mismos datos produce el mismo timestamp y la
    restricción uq_trafico_calles_ciclo descarta el duplicado (P6).
    """
    return gdf_fuentes["timestamp_lectura"].max().to_pydatetime()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# v3 — vector tiles
# ---------------------------------------------------------------------------
def leer_flujo_reciente(engine) -> pd.DataFrame:
    """Aristas medidas en la última pasada de tiles, si es de los últimos VENTANA_TILES."""
    sql = text("""
        WITH ultima AS (
            SELECT max(timestamp_lectura) AS ts
            FROM fact_flujo_aristas
            WHERE timestamp_lectura >= :desde
        )
        SELECT f.osm_id, f.nivel_relativo, f.cerrada, f.timestamp_lectura
        FROM fact_flujo_aristas f
        JOIN ultima u ON f.timestamp_lectura = u.ts
    """)
    with engine.connect() as conn:
        filas = conn.execute(sql, {"desde": _ahora() - VENTANA_TILES}).fetchall()
    return pd.DataFrame(filas, columns=["osm_id", "nivel_relativo", "cerrada", "timestamp_lectura"])


def congestion_desde_nivel(nivel: np.ndarray, cerrada: np.ndarray) -> np.ndarray:
    """traffic_level (velocidad actual / libre) → nivel_congestion (tiempo actual / libre) en [1, CONGESTION_MAX]."""
    nivel = np.asarray(nivel, dtype=float)
    with np.errstate(divide="ignore"):
        congestion = np.where(nivel > 0, 1.0 / nivel, CONGESTION_MAX)
    congestion = np.clip(congestion, 1.0, CONGESTION_MAX)
    return np.where(np.asarray(cerrada, dtype=bool), CONGESTION_MAX, congestion)


def inferir_desde_tiles(gdf_calles: gpd.GeoDataFrame, flujo: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """
    Congestión para cada calle de gdf_calles (mismo orden):
      - medida en la pasada → valor directo ('TILE')
      - sin medir → IDW desde las aristas medidas NO cerradas ('IDW_TILE')
    """
    medidas = flujo.assign(
        congestion=congestion_desde_nivel(flujo["nivel_relativo"].to_numpy(), flujo["cerrada"].to_numpy())
    ).set_index("osm_id")
    medidas = medidas[~medidas.index.duplicated()]

    es_medida = gdf_calles["osm_id"].isin(medidas.index).to_numpy()
    congestion = np.ones(len(gdf_calles))
    congestion[es_medida] = medidas.loc[gdf_calles.loc[es_medida, "osm_id"], "congestion"].to_numpy()

    abiertas = gdf_calles["osm_id"].isin(medidas.index[~medidas["cerrada"].astype(bool)]).to_numpy()
    fuentes = gdf_calles[abiertas].assign(nivel_congestion=congestion[abiertas])
    objetivo = gdf_calles[~es_medida]
    if len(objetivo) and len(fuentes):
        congestion[~es_medida] = aplicar_idw(objetivo, fuentes, K_VECINOS, POTENCIA_IDW, DISTANCIA_MAX_M,
                                             limites=(1.0, CONGESTION_MAX))

    log.info(f"  → {es_medida.sum():,} calles medidas por tiles, {(~es_medida).sum():,} inferidas por IDW "
             f"({len(fuentes):,} fuentes abiertas)")
    metodos = ["TILE" if m else "IDW_TILE" for m in es_medida]
    return congestion, metodos


def ciclo_ya_calculado(engine, timestamp_ref: datetime) -> bool:
    """True si trafico_calles ya tiene la inferencia de este estado del tráfico."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM trafico_calles WHERE timestamp_calculo = :ts)"),
            {"ts": timestamp_ref},
        ).scalar()


# ---------------------------------------------------------------------------
# Etapa 2 — Muestrear puntos equidistantes sobre cada segmento
# ---------------------------------------------------------------------------

# Distancia en metros entre puntos de muestreo sobre cada LineString.
# 150m significa que un segmento de 600m genera 4 puntos fuente en lugar de 1.
# Valores menores = cobertura más densa = heatmap más continuo.
MUESTREO_LINEA_M = 100

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
                "geom":             linea.centroid,
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
# Etapa 3 — Leer los centroides de la red vial OSM
# ---------------------------------------------------------------------------

def leer_centroides_osm(engine) -> gpd.GeoDataFrame:
    """
    Trae todos los segmentos de dim_calles_osm con su centroide
    precalculado en metros (columna centroide_m, poblada por osmnx_loader.py)

    No se recalcula el centroide aqui, osmnx_loader ya hizo la reproyeccion una sola vez al cargar
    red vial. Reproyectando mas de 24k geometrias en cada ciclo ETL cada 15min seria innecesario,
    leemos el valor calculado ya registrado en tablas.

    Retorna: GeoDataForm con columnas:
        osm_id, nombre, tipo_via, geom (Point en EPSG:32614, columna activa)
    """
    log.info(f"Etapa 3: leyendo centroides de la red vial OSM (dim_calles_osm)...")

    sql = """
        SELECT
            osm_id,
            nombre,
            tipo_via,
            centroide_m
        FROM dim_calles_osm
        WHERE centroide_m IS NOT NULL
          AND activo
    """
    with engine.connect() as conn:
        conexion_nativa = conn.connection
        gdf = gpd.read_postgis(sql, con=conexion_nativa, geom_col="centroide_m")

    #read_postgis ya sabe que centroide_m esta en ESPG:32614 pq
    #dim_calles_osm lo declaro asi al insertar (ST_GeomFromText con SRID 32614), verificamos
    if gdf.crs is None or gdf.crs.to_epsg() != SRID_METRICO:
        gdf = gdf.set_crs(epsg=SRID_METRICO, allow_override=True)

    log.info(f"  → {len(gdf):,} calles OSM cargadas como puntos objetivo.")
    return gdf


# ---------------------------------------------------------------------------
# Etapa 4 — Aplicar IDW (el corazón del script)
# ---------------------------------------------------------------------------

def aplicar_idw(
    gdf_objetivo: gpd.GeoDataFrame,
    gdf_fuentes: gpd.GeoDataFrame,
    k: int,
    potencia: float,
    distancia_max: float,
    limites: tuple[float, float] = (1.0, 3.0),
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

    Retorna un array de forma (n_puntos_grilla,) recortado a `limites`
    ([1, 3] en la v2; [1, CONGESTION_MAX] en la v3).
    """
    log.info(f"Etapa 4: aplicando IDW (K={k}, p={potencia})...")

    # Coordenadas como arrays numpy: forma (n, 2)
    coords_fuentes = np.array(
        [geom.coords[0] for geom in gdf_fuentes.geometry]
    )
    coords_objetivo = np.array(
        [geom.coords[0] for geom in gdf_objetivo.geometry]
    )
    valores_fuentes = gdf_fuentes["nivel_congestion"].values.astype(float)

    # Árbol espacial sobre las fuentes
    tree = cKDTree(coords_fuentes)

    # Consulta: K vecinos más cercanos para cada punto objetivo
    # distances: (n_objetivo, K), indices: (n_objetivo, K)
    distancias, indices = tree.query(coords_objetivo, k=min(k, len(gdf_fuentes)))

    #Si k=1, scipy devuelve arrays 1D en lugar de 2D, normalizamos forma
    #para que el resto del codigo funcione igual al asumir columnas k
    if distancias.ndim == 1:
        distancias = distancias.reshape(-1,1)
        indices = indices.reshape(-1,1)

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

    congestion_inferida = np.clip(congestion_inferida, *limites)

    log.info(
        f"  → Congestión inferida: "
        f"min={congestion_inferida.min():.3f}, "
        f"media={congestion_inferida.mean():.3f}, "
        f"max={congestion_inferida.max():.3f}"
    )
    return congestion_inferida


# ---------------------------------------------------------------------------
# Etapa 5 — Escribir resultados en trafico_calles
# ---------------------------------------------------------------------------

_trafico_calles = Table(
    "trafico_calles", MetaData(),
    Column("timestamp_calculo", DateTime(timezone=True)),
    Column("osm_id", BigInteger),
    Column("nivel_congestion_inf", Float),
    Column("metodo_inferencia", Text),
)


def guardar_resultados(
    engine,
    gdf_objetivo: gpd.GeoDataFrame,
    congestion_inferida: np.ndarray,
    timestamp_calculo: datetime,
    metodos="IDW_OSM",
) -> None:
    """
    Inserta los puntos inferidos en la tabla trafico_calles.

    A diferencia de lo que habia de la version 1 con trafico_inferido con
    lat y lon sueltos, aqui cada fila referencia un osm_id real de 
    dim_calles_osm, Grafana asi podra hacer JOIN para obtener nombre,
    tipo de via y geometria completa de cada calle.

    Idempotente (P6): timestamp_calculo es la referencia del ciclo y el INSERT
    descarta las filas (timestamp_calculo, osm_id) que ya existan.

    metodos: un texto para todas las filas o una secuencia alineada con gdf_objetivo.

    Rendimiento: un INSERT de SQLAlchemy Core con lista de dicts usa
    "insertmanyvalues" (INSERT multi-fila por lotes). La versión anterior
    (iterrows + text() fila a fila) tardaba ~30 s con 67 k calles.
    """
    log.info("Etapa 5: guardando resultados en trafico_calles...")

    ids = gdf_objetivo["osm_id"].to_numpy()
    if isinstance(metodos, str):
        metodos = [metodos] * len(ids)
    registros = [
        {
            "timestamp_calculo": timestamp_calculo,
            "osm_id": int(osm_id),
            "nivel_congestion_inf": float(c),
            "metodo_inferencia": metodo,
        }
        for osm_id, c, metodo in zip(ids, congestion_inferida, metodos)
    ]

    # RETURNING: con insertmanyvalues, rowcount solo refleja el último lote;
    # contar las filas devueltas da el total real insertado.
    stmt = (pg_insert(_trafico_calles)
            .on_conflict_do_nothing(constraint="uq_trafico_calles_ciclo")
            .returning(_trafico_calles.c.osm_id))
    total = len(registros)
    with engine.begin() as conn:
        insertados = len(conn.execute(stmt, registros).fetchall())

    log.info(
        f"  → {insertados:,}/{total:,} calles insertadas en trafico_calles "
        f"(el resto ya existía para este ciclo)"
    )


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------

def ejecutar(engine):
    """
    Punto de entrada. v3 (tiles) si hay una pasada reciente; si no, v2 (Segment Data).
    Puede llamarse directamente o importarse desde el ETL principal.
    """
    log.info("=== predictor.py iniciando ===")

    flujo = leer_flujo_reciente(engine)
    if not flujo.empty:
        _ejecutar_desde_tiles(engine, flujo)
    else:
        log.info(f"Sin pasada de tiles en los últimos {VENTANA_TILES}: respaldo con Segment Data (v2).")
        _ejecutar_desde_segment_data(engine)

    log.info("=== predictor.py finalizado ===")


def _ejecutar_desde_tiles(engine, flujo: pd.DataFrame) -> None:
    timestamp = pd.Timestamp(flujo["timestamp_lectura"].max()).to_pydatetime()
    if ciclo_ya_calculado(engine, timestamp):
        log.info(f"La pasada de tiles de {timestamp.isoformat()} ya fue inferida; se omite.")
        return
    log.info(f"v3: infiriendo desde la pasada de tiles de {timestamp.isoformat()} ({len(flujo):,} aristas medidas)")

    gdf_calles_osm = leer_centroides_osm(engine)
    congestion, metodos = inferir_desde_tiles(gdf_calles_osm, flujo)
    guardar_resultados(engine, gdf_calles_osm, congestion, timestamp, metodos=metodos)


def _ejecutar_desde_segment_data(engine) -> None:
    # Etapa 1
    gdf_fuentes_geo = leer_lecturas_recientes(engine)
    if gdf_fuentes_geo.empty:
        log.warning("Sin lecturas con geometría: no hay nada que inferir.")
        return

    timestamp = referencia_ciclo(gdf_fuentes_geo)
    if _ahora() - timestamp > ANTIGUEDAD_MAXIMA:
        log.warning(
            f"La lectura más reciente es de {timestamp.isoformat()} (más de "
            f"{ANTIGUEDAD_MAXIMA.days} días): no hay tráfico actual que inferir. "
            "¿El ETL está obteniendo datos de TomTom?"
        )
        return
    if ciclo_ya_calculado(engine, timestamp):
        log.info(f"El estado del tráfico de {timestamp.isoformat()} ya fue inferido; se omite.")
        return
    log.info(f"Infiriendo el estado del tráfico de {timestamp.isoformat()}")

    # Etapa 2
    gdf_fuentes_m = muestrear_puntos_linea(gdf_fuentes_geo)

    # Etapa 3
    gdf_calles_osm = leer_centroides_osm(engine)

    # Etapa 4
    congestion = aplicar_idw(gdf_calles_osm, gdf_fuentes_m, K_VECINOS, POTENCIA_IDW, DISTANCIA_MAX_M)

    # Etapa 5
    guardar_resultados(engine, gdf_calles_osm, congestion, timestamp)


if __name__ == "__main__":
    from sqlalchemy import create_engine
    motor_prueba = create_engine(DB_URL)
    ejecutar(motor_prueba)
