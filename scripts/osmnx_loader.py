"""
osmnx_loader.py
===============
Script de carga única (o actualización periódica) de la red vial de SLP
desde OpenStreetMap hacia la tabla dim_calles_osm.

Posición en el sistema:
    OpenStreetMap  →  osmnx_loader.py  →  dim_calles_osm  →  predictor.py

Cuándo ejecutarlo:
    - Una vez al arrancar el proyecto por primera vez.
    - Cuando la mancha urbana cambie significativamente (cada varios meses).
    - NO en cada ciclo del ETL — la red vial es estática a escala diaria.

Dependencias:
    pip install osmnx geopandas shapely sqlalchemy psycopg2-binary

Concepto clave — qué es un grafo vial:
    osmnx descarga la red vial como un grafo G donde:
      - Nodos  = intersecciones (un punto lat/lon)
      - Aristas = segmentos de calle entre dos intersecciones (un LineString)
    Nosotros solo necesitamos las ARISTAS porque son las que tienen
    geometría lineal, nombre y tipo de vía. Los nodos se descartan.
"""

import logging
from sqlalchemy import create_engine, text
import geopandas as gpd
import osmnx as ox
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
# Parámetros
# ---------------------------------------------------------------------------

DB_URL = config.DB_URL

# Lugar de búsqueda. osmnx acepta nombres de lugar en lenguaje natural —
# internamente los resuelve contra Nominatim (el geocodificador de OSM).
# "San Luis Potosí, Mexico" descarga la zona metropolitana completa.
#LUGAR = "San Luis Potosí, Mexico"

# Lugar de búsqueda. IMPORTANTE: "San Luis Potosí, Mexico" es ambiguo —
# Nominatim puede resolverlo contra el ESTADO (que es 36x más grande que
# el municipio) en lugar de la ciudad. Para evitar esa ambigüedad usamos
# un bounding box explícito, el mismo que usa predictor.py para la grilla
# IDW. Esto además garantiza que ambos sistemas cubran exactamente la
# misma zona geográfica.
BBOX = (-101.03, 22.11, -100.96, 22.19)  # (lon_min, lat_min, lon_max, lat_max)

# Tipos de vía a incluir. OSM clasifica las calles en jerarquías:
#   motorway/trunk   → autopistas y vías rápidas
#   primary          → avenidas principales (las que ya mides con TomTom)
#   secondary        → avenidas secundarias importantes
#   tertiary         → calles conectoras entre colonias
#   residential      → calles internas de colonias
#   unclassified     → calles sin clasificar pero transitables
#
# Excluimos: footway, cycleway, path, steps (peatonal/ciclista),
#            service (estacionamientos, accesos privados),
#            track (caminos de terracería)
#
# Para SLP, residential + tertiary + secondary cubre bien la zona urbana
# sin saturar de calles de acceso privado o caminos rurales.
TIPOS_VIA = {
    "motorway", "trunk", "primary", "secondary",
    "tertiary", "residential", "unclassified",
    "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
}

# SRID métrico para SLP — el mismo que usa predictor.py
SRID_METRICO = 32614


# ---------------------------------------------------------------------------
# Etapa 1 — Descargar la red vial desde OpenStreetMap
# ---------------------------------------------------------------------------

def descargar_red_vial(bbox: tuple, tipos: set) -> gpd.GeoDataFrame:
    """
    Descarga la red vial del lugar especificado usando osmnx.

    osmnx.graph_from_place() hace internamente tres cosas:
      1. Geocodifica el nombre del lugar contra Nominatim → obtiene el
         polígono administrativo de SLP.
      2. Construye una query Overpass para pedir todas las vías dentro
         de ese polígono (similar a lo que hace tu ETL actual, pero
         automático y sin definir coordenadas manualmente).
      3. Retorna un grafo NetworkX con nodos y aristas.

    Luego graph_to_gdfs() convierte ese grafo en dos GeoDataFrames:
      - nodes_gdf: intersecciones (no lo necesitamos)
      - edges_gdf: segmentos de calle (esto es lo que queremos)

    Cada fila de edges_gdf es un segmento con columnas como:
      name, highway, length, geometry (LineString en WGS84)
    """
    log.info(f"Descargando red vial del bbox '{bbox}' desde OpenStreetMap...")
    log.info("  (esto puede tardar 1-3 minutos dependiendo del tamaño de la ciudad)")

    lon_min, lat_min, lon_max, lat_max = bbox

    # network_type="drive" filtra solo vías para vehículos motorizados.
    # Esto ya excluye peatonal/ciclista automáticamente antes de nuestro
    # filtro por tipo_via.
    grafo = ox.graph_from_bbox(
        #lugar,
        bbox=(lon_min,lat_min,lon_max,lat_max),
        network_type="drive",
        retain_all=False,     # solo el componente conectado principal
        simplify=True,        # simplifica la geometría (elimina nodos intermedios
                              # que no son intersecciones reales — reduce ruido)
    )

    # Convertir a GeoDataFrame. Solo necesitamos las aristas (edges).
    _, edges_gdf = ox.graph_to_gdfs(grafo)

    log.info(f"  → {len(edges_gdf):,} segmentos descargados antes de filtrar")
    return edges_gdf


# ---------------------------------------------------------------------------
# Etapa 2 — Limpiar y filtrar
# ---------------------------------------------------------------------------

def limpiar_red(edges_gdf: gpd.GeoDataFrame, tipos: set) -> gpd.GeoDataFrame:
    """
    Filtra, renombra y prepara el GeoDataFrame para inserción en la DB.

    Columnas que vienen de osmnx que nos interesan:
      - osmid (índice): ID único de OSM para cada arista
      - name: nombre de la calle (puede ser lista si la arista tiene varios nombres)
      - highway: tipo de vía ("residential", "primary", etc.)
      - length: longitud en metros (ya la calcula osmnx)
      - geometry: LineString en WGS84

    Columnas que creamos nosotros:
      - centroide_m: punto central del segmento en UTM 14N (para el IDW)
    """
    log.info("Etapa 2: limpiando y filtrando la red vial...")

    # Resetear índice para que osmid sea una columna normal, no el índice
    gdf = edges_gdf.reset_index()

    # osmnx usa un índice compuesto (u, v, key) para identificar aristas.
    # u y v son los nodos de inicio y fin. Para nosotros el identificador
    # relevante es osmid (el ID de OpenStreetMap de la vía subyacente).
    # Como una misma vía OSM puede tener múltiples aristas en el grafo,
    # generamos un ID único combinando u, v y key.
    gdf["osm_id"] = (
        gdf["u"].astype(str) + "_" +
        gdf["v"].astype(str) + "_" +
        gdf["key"].astype(str)
    ).apply(hash).abs()

    # Normalizar el nombre: puede ser string, lista de strings, o NaN.
    # Si es lista (arista con varios nombres), tomamos el primero.
    def normalizar_nombre(valor):
        if isinstance(valor, list):
            return valor[0]
        if isinstance(valor, str):
            return valor
        return None  # NaN → NULL en la DB

    gdf["nombre"] = gdf["name"].apply(normalizar_nombre)

    # Normalizar tipo_via: también puede ser lista (MultiPolygon de tipos)
    def normalizar_tipo(valor):
        if isinstance(valor, list):
            return valor[0]
        return str(valor)

    gdf["tipo_via"] = gdf["highway"].apply(normalizar_tipo)

    # Filtrar por tipos de vía relevantes
    antes = len(gdf)
    gdf = gdf[gdf["tipo_via"].isin(tipos)].copy()
    log.info(f"  → {antes:,} → {len(gdf):,} segmentos tras filtrar por tipo de vía")

    # Calcular centroide en UTM 14N (EPSG:32614).
    # Lo precalculamos aquí para no hacerlo en cada ciclo del predictor.
    # to_crs() reproyecta toda la columna geometry a metros.
    gdf_metrico = gdf.to_crs(epsg=SRID_METRICO)
    gdf["centroide_m"] = gdf_metrico.geometry.centroid

    # Seleccionar solo las columnas que necesita la DB
    gdf_final = gdf[[
        "osm_id", "nombre", "tipo_via", "length", "geometry", "centroide_m"
    ]].rename(columns={"length": "longitud_m"})

    # Eliminar duplicados por osm_id (pueden aparecer si el grafo tiene
    # aristas paralelas entre los mismos nodos)
    gdf_final = gdf_final.drop_duplicates(subset="osm_id")

    log.info(f"  → {len(gdf_final):,} segmentos únicos listos para insertar")
    return gdf_final


# ---------------------------------------------------------------------------
# Etapa 3 — Insertar en dim_calles_osm
# ---------------------------------------------------------------------------

def insertar_en_db(gdf: gpd.GeoDataFrame, engine) -> None:
    """
    Inserta la red vial limpia en dim_calles_osm.

    Estrategia: TRUNCATE + INSERT.
    Como dim_calles_osm es estática, lo más simple es vaciarla y repoblarla.
    Esto garantiza consistencia si el script se corre como actualización.

    Por qué no usamos gdf.to_postgis() directamente:
    Necesitamos insertar DOS geometrías por fila (geom y centroide_m) en
    diferentes columnas con diferentes SRIDs. gdf.to_postgis() solo maneja
    una columna de geometría. Usamos psycopg2 via SQLAlchemy con WKT.
    """
    log.info("Etapa 3: insertando en dim_calles_osm...")

    # Convertir geometrías a WKT (Well-Known Text) para el INSERT
    # ST_GeomFromText(wkt, srid) las convierte de vuelta a geometría PostGIS
    from shapely import wkt as shapely_wkt

    registros = []
    for _, fila in gdf.iterrows():
        registros.append({
            "osm_id":      int(fila["osm_id"]),
            "nombre":      fila["nombre"],
            "tipo_via":    fila["tipo_via"],
            "longitud_m":  float(fila["longitud_m"]) if fila["longitud_m"] else None,
            "geom_wkt":    fila["geometry"].wkt,
            "centro_wkt":  fila["centroide_m"].wkt,
        })

    insert_sql = text("""
        INSERT INTO dim_calles_osm
            (osm_id, nombre, tipo_via, longitud_m, geom, centroide_m)
        VALUES (
            :osm_id,
            :nombre,
            :tipo_via,
            :longitud_m,
            ST_GeomFromText(:geom_wkt,   4326),
            ST_GeomFromText(:centro_wkt, 32614)
        )
        ON CONFLICT (osm_id) DO UPDATE SET
            nombre     = EXCLUDED.nombre,
            tipo_via   = EXCLUDED.tipo_via,
            longitud_m = EXCLUDED.longitud_m,
            geom       = EXCLUDED.geom,
            centroide_m = EXCLUDED.centroide_m
    """)

    # Insertar en lotes de 500 para no saturar la memoria de la transacción
    LOTE = 500
    total = len(registros)
    insertados = 0

    with engine.begin() as conn:
        for i in range(0, total, LOTE):
            lote = registros[i:i + LOTE]
            conn.execute(insert_sql, lote)
            insertados += len(lote)
            log.info(f"  → {insertados:,}/{total:,} segmentos insertados...")

    log.info(f"  → Carga completa: {total:,} segmentos en dim_calles_osm")


# ---------------------------------------------------------------------------
# Etapa 4 — Resumen estadístico
# ---------------------------------------------------------------------------

def mostrar_resumen(engine) -> None:
    """
    Muestra un resumen de lo que quedó en la tabla, útil para decidir
    qué tipos de vía incluir en las consultas de Grafana y el predictor.
    """
    log.info("Etapa 4: resumen de la red vial cargada...")

    sql = text("""
        SELECT
            tipo_via,
            COUNT(*)                        AS segmentos,
            ROUND(SUM(longitud_m)::numeric / 1000, 1) AS km_totales,
            COUNT(*) FILTER (WHERE nombre IS NOT NULL) AS con_nombre
        FROM dim_calles_osm
        GROUP BY tipo_via
        ORDER BY segmentos DESC
    """)

    with engine.connect() as conn:
        resultado = conn.execute(sql).fetchall()

    log.info("  Tipo de vía        | Segmentos | Km totales | Con nombre")
    log.info("  ─────────────────────────────────────────────────────────")
    for fila in resultado:
        log.info(
            f"  {fila[0]:<18} | {fila[1]:>9,} | {fila[2]:>10} | {fila[3]:>10,}"
        )


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------

def ejecutar():
    log.info("=== osmnx_loader.py iniciando ===")

    engine = create_engine(DB_URL, pool_pre_ping=True)

    # Etapa 1: descargar de OSM
    edges_gdf = descargar_red_vial(BBOX, TIPOS_VIA)

    # Etapa 2: limpiar y preparar
    gdf_limpio = limpiar_red(edges_gdf, TIPOS_VIA)

    # Etapa 3: insertar en DB
    insertar_en_db(gdf_limpio, engine)

    # Etapa 4: resumen
    mostrar_resumen(engine)

    log.info("=== osmnx_loader.py finalizado ===")
    log.info("Siguiente paso: modificar predictor.py para usar dim_calles_osm")


if __name__ == "__main__":
    ejecutar()
