"""
osmnx_loader.py
===============
Carga (o actualiza) la red vial de SLP desde OpenStreetMap en dim_calles_osm
y la topología de ruteo en dim_calles_osm_vertices_pgr.

Posición en el sistema:
    OpenStreetMap → osmnx_loader.py → dim_calles_osm (+ vértices) → predictor.py / pgRouting

Cuándo ejecutarlo:
    - Al montar el proyecto por primera vez (después de las migraciones).
    - Cuando la red vial cambie (cada varios meses). NO en cada ciclo del ETL.

Requisitos:
    Migraciones aplicadas hasta la 005 (`python src/migrate.py`).
    Ejecutar desde la raíz del repo para reutilizar la caché de osmnx en ./cache:
        python scripts/osmnx_loader.py

Identidad de las aristas (P3/P4 de docs/DATABASE.md):
    osmnx representa la red como un grafo dirigido. Cada arista se identifica
    por (u, v, k): nodo OSM inicial, nodo OSM final e índice de arista paralela.
    Una calle de doble sentido produce dos aristas (u→v y v→u).
    - (u, v, k) es la clave de upsert → re-ejecutar no duplica la red.
    - osm_id = blake2b(u_v_k): estable entre ejecuciones (antes era hash() de Python).
    - source/target = u/v → la topología sale directamente de OSM, sin
      reconstruirla a mano con pgRouting.
    - Las filas cargadas con la versión anterior (u NULL) se "adoptan" si su
      geometría coincide con una arista nueva, conservando su osm_id para no
      romper el histórico de trafico_calles.
    - Las aristas que ya no existen en OSM se marcan activo = FALSE (no se
      borran, porque trafico_calles las referencia).
"""

import logging
import sys
from pathlib import Path

import geopandas as gpd
import osmnx as ox
from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, MetaData, Table, Text, create_engine, insert, text

import config

# src/ al FINAL del path: así `config` sigue resolviendo a scripts/config.py.
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))
import zona  # noqa: E402
from osm_utils import id_determinista, normalizar_nombre, normalizar_oneway, normalizar_tipo, parsear_maxspeed

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

# Zona: bounding box explícito y compartido con los puntos TomTom (src/zona.py).
# No usar un nombre de lugar: "San Luis Potosí, Mexico" es ambiguo en Nominatim.

# Tipos de vía incluidos. Se excluyen peatonales, ciclovías, service y track.
TIPOS_VIA = {
    "motorway", "trunk", "primary", "secondary",
    "tertiary", "residential", "unclassified",
    "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
}

SRID_METRICO = 32614
MIGRACION_REQUERIDA = "005"

# Tablas temporales de staging (se crean con SQL en la transacción de carga)
_meta_staging = MetaData()
_stg_aristas = Table(
    "stg_aristas", _meta_staging,
    Column("osm_id", BigInteger), Column("u", BigInteger), Column("v", BigInteger),
    Column("k", Integer), Column("nombre", Text), Column("tipo_via", Text),
    Column("longitud_m", Float), Column("maxspeed_kmh", Float), Column("oneway", Boolean),
    Column("geom_wkt", Text), Column("centro_wkt", Text),
)
_stg_nodos = Table(
    "stg_nodos", _meta_staging,
    Column("id", BigInteger), Column("x", Float), Column("y", Float),
)


# ---------------------------------------------------------------------------
# Etapa 1 — Descargar la red vial desde OpenStreetMap
# ---------------------------------------------------------------------------
def descargar_red_vial(bbox: tuple) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Descarga el grafo vial dirigido (network_type="drive") y lo devuelve como
    (nodos, aristas). Las aristas conservan u, v, key como columnas.
    """
    log.info(f"Etapa 1: descargando red vial del bbox {bbox} desde OpenStreetMap...")
    grafo = ox.graph_from_bbox(
        bbox=bbox,
        network_type="drive",
        retain_all=False,  # solo el componente conectado principal
        simplify=True,     # quita nodos intermedios que no son intersecciones
    )
    nodos, aristas = ox.graph_to_gdfs(grafo)
    log.info(f"  → {len(nodos):,} nodos y {len(aristas):,} aristas descargadas")
    return nodos, aristas.reset_index()


# ---------------------------------------------------------------------------
# Etapa 2 — Limpiar y filtrar
# ---------------------------------------------------------------------------
def limpiar_red(nodos: gpd.GeoDataFrame, aristas: gpd.GeoDataFrame, tipos: set) -> tuple[list[dict], list[dict]]:
    """Filtra por tipo de vía y prepara los registros de aristas y nodos para staging."""
    log.info("Etapa 2: limpiando y filtrando la red vial...")

    gdf = aristas.copy()
    gdf["tipo_via"] = gdf["highway"].apply(normalizar_tipo)
    antes = len(gdf)
    gdf = gdf[gdf["tipo_via"].isin(tipos)].copy()
    log.info(f"  → {antes:,} → {len(gdf):,} aristas tras filtrar por tipo de vía")

    gdf["centroide_m"] = gdf.to_crs(epsg=SRID_METRICO).geometry.centroid

    columna = lambda nombre: gdf[nombre] if nombre in gdf.columns else [None] * len(gdf)  # noqa: E731

    aristas_reg = []
    vistos = set()
    for u, v, k, nombre, tipo, longitud, maxspeed, oneway, geom, centro in zip(
        gdf["u"], gdf["v"], gdf["key"], columna("name"), gdf["tipo_via"], gdf["length"],
        columna("maxspeed"), columna("oneway"), gdf.geometry, gdf["centroide_m"],
    ):
        clave = (int(u), int(v), int(k))
        if clave in vistos:
            continue
        vistos.add(clave)
        aristas_reg.append({
            "osm_id": id_determinista(*clave),
            "u": clave[0], "v": clave[1], "k": clave[2],
            "nombre": normalizar_nombre(nombre),
            "tipo_via": tipo,
            "longitud_m": float(longitud) if longitud is not None else None,
            "maxspeed_kmh": parsear_maxspeed(maxspeed),
            "oneway": normalizar_oneway(oneway),
            "geom_wkt": geom.wkt,
            "centro_wkt": centro.wkt,
        })

    usados = {a["u"] for a in aristas_reg} | {a["v"] for a in aristas_reg}
    nodos_reg = [
        {"id": int(osmid), "x": float(x), "y": float(y)}
        for osmid, x, y in zip(nodos.index, nodos["x"], nodos["y"])
        if int(osmid) in usados
    ]

    log.info(f"  → {len(aristas_reg):,} aristas únicas y {len(nodos_reg):,} nodos listos")
    return aristas_reg, nodos_reg


# ---------------------------------------------------------------------------
# Etapa 3 — Sincronizar con la base de datos
# ---------------------------------------------------------------------------
def _verificar_migraciones(conn) -> None:
    existe = conn.execute(text("SELECT to_regclass('schema_migrations') IS NOT NULL")).scalar()
    version = conn.execute(text("SELECT max(version) FROM schema_migrations")).scalar() if existe else None
    if version is None or version < MIGRACION_REQUERIDA:
        raise RuntimeError(
            f"La BD está en la migración {version!r}; se requiere >= {MIGRACION_REQUERIDA}. "
            "Ejecuta primero: python src/migrate.py"
        )


def sincronizar_db(aristas: list[dict], nodos: list[dict], engine) -> None:
    """
    Una sola transacción: staging → adopción de filas antiguas → upsert de aristas
    → desactivación de aristas que ya no existen → upsert/limpieza de vértices.
    Si algo falla, no se modifica nada.
    """
    log.info("Etapa 3: sincronizando dim_calles_osm y vértices...")

    with engine.begin() as conn:
        _verificar_migraciones(conn)

        # 3.1 Staging
        conn.execute(text("""
            CREATE TEMP TABLE stg_aristas (
                osm_id bigint, u bigint, v bigint, k integer, nombre text, tipo_via text,
                longitud_m double precision, maxspeed_kmh double precision, oneway boolean,
                geom_wkt text, centro_wkt text
            ) ON COMMIT DROP;
            CREATE TEMP TABLE stg_nodos (id bigint, x double precision, y double precision) ON COMMIT DROP;
        """))
        conn.execute(insert(_stg_aristas), aristas)
        conn.execute(insert(_stg_nodos), nodos)
        conn.execute(text("""
            ALTER TABLE stg_aristas
                ADD COLUMN geom geometry(LineString, 4326),
                ADD COLUMN centroide geometry(Point, 32614);
            UPDATE stg_aristas
               SET geom = ST_GeomFromText(geom_wkt, 4326),
                   centroide = ST_GeomFromText(centro_wkt, 32614);
            CREATE INDEX ON stg_aristas USING gist (geom);
            CREATE INDEX ON stg_aristas (u, v, k);
            ANALYZE stg_aristas;
        """))

        # 3.2 Adopción de filas de la versión anterior (u NULL): misma geometría,
        # mismo sentido → se les asigna (u, v, k) y conservan su osm_id.
        # Cada pasada empareja 1:1; se repite porque las aristas paralelas
        # (mismos nodos, k = 0, 1, ...) tienen geometrías idénticas y forman
        # grupos de varios candidatos que se resuelven de a una pareja por pasada.
        adoptadas = 0
        while True:
            en_pasada = _adoptar_una_pasada(conn)
            adoptadas += en_pasada
            if en_pasada == 0:
                break
        log.info(f"  → {adoptadas:,} aristas existentes adoptadas (conservan su osm_id)")
        _upsert_y_limpieza(conn, len(nodos))


def _adoptar_una_pasada(conn) -> int:
    return conn.execute(text("""
            WITH candidatos AS (
                SELECT c.osm_id, s.u, s.v, s.k,
                       row_number() OVER (PARTITION BY c.osm_id ORDER BY s.u, s.v, s.k) AS rc,
                       row_number() OVER (PARTITION BY s.u, s.v, s.k ORDER BY c.osm_id) AS rs
                FROM dim_calles_osm c
                JOIN stg_aristas s
                  ON c.geom && s.geom
                 AND c.tipo_via = s.tipo_via
                 AND ST_NPoints(c.geom) = ST_NPoints(s.geom)
                 AND ST_DWithin(ST_StartPoint(c.geom), ST_StartPoint(s.geom), 1e-6)
                 AND ST_DWithin(ST_EndPoint(c.geom),   ST_EndPoint(s.geom),   1e-6)
                WHERE c.u IS NULL
                  AND NOT EXISTS (SELECT 1 FROM dim_calles_osm x
                                  WHERE x.u = s.u AND x.v = s.v AND x.k = s.k)
            )
            UPDATE dim_calles_osm c
               SET u = m.u, v = m.v, k = m.k
              FROM candidatos m
             WHERE c.osm_id = m.osm_id AND m.rc = 1 AND m.rs = 1
        """)).rowcount


def _upsert_y_limpieza(conn, n_nodos: int) -> None:
    # 3.3 Upsert por clave natural (u, v, k)
    resultado = conn.execute(text("""
        INSERT INTO dim_calles_osm AS c
            (osm_id, u, v, k, source, target, nombre, tipo_via, longitud_m,
             maxspeed_kmh, oneway, geom, centroide_m, activo)
        SELECT osm_id, u, v, k, u, v, nombre, tipo_via, longitud_m,
               maxspeed_kmh, oneway, geom, centroide, TRUE
        FROM stg_aristas
        ON CONFLICT (u, v, k) DO UPDATE SET
            source       = EXCLUDED.source,
            target       = EXCLUDED.target,
            nombre       = EXCLUDED.nombre,
            tipo_via     = EXCLUDED.tipo_via,
            longitud_m   = EXCLUDED.longitud_m,
            maxspeed_kmh = EXCLUDED.maxspeed_kmh,
            oneway       = EXCLUDED.oneway,
            geom         = EXCLUDED.geom,
            centroide_m  = EXCLUDED.centroide_m,
            activo       = TRUE
        RETURNING (xmax = 0) AS insertada
    """)).fetchall()
    insertadas = sum(1 for fila in resultado if fila.insertada)
    log.info(f"  → {insertadas:,} aristas nuevas, {len(resultado) - insertadas:,} actualizadas")

    # 3.4 Aristas que ya no están en OSM (o que no se pudieron adoptar)
    desactivadas = conn.execute(text("""
        UPDATE dim_calles_osm c
           SET activo = FALSE, source = NULL, target = NULL
         WHERE c.activo
           AND NOT EXISTS (SELECT 1 FROM stg_aristas s
                           WHERE s.u = c.u AND s.v = c.v AND s.k = c.k)
    """)).rowcount
    log.info(f"  → {desactivadas:,} aristas marcadas como inactivas")

    # 3.5 Vértices = nodos OSM usados por aristas activas
    conn.execute(text("""
        INSERT INTO dim_calles_osm_vertices_pgr (id, x, y, geom)
        SELECT id, x, y, ST_SetSRID(ST_MakePoint(x, y), 4326) FROM stg_nodos
        ON CONFLICT (id) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y, geom = EXCLUDED.geom
    """))
    borrados = conn.execute(text("""
        DELETE FROM dim_calles_osm_vertices_pgr p
         WHERE NOT EXISTS (SELECT 1 FROM dim_calles_osm c WHERE c.activo AND c.source = p.id)
           AND NOT EXISTS (SELECT 1 FROM dim_calles_osm c WHERE c.activo AND c.target = p.id)
    """)).rowcount
    log.info(f"  → {n_nodos:,} vértices sincronizados, {borrados:,} vértices obsoletos eliminados")


# ---------------------------------------------------------------------------
# Etapa 4 — Validación y resumen
# ---------------------------------------------------------------------------
def validar_y_resumir(engine) -> None:
    log.info("Etapa 4: validando topología...")
    with engine.connect() as conn:
        huerfanas = conn.execute(text("""
            SELECT count(*) FROM dim_calles_osm c
            WHERE c.activo AND (
                NOT EXISTS (SELECT 1 FROM dim_calles_osm_vertices_pgr p WHERE p.id = c.source) OR
                NOT EXISTS (SELECT 1 FROM dim_calles_osm_vertices_pgr p WHERE p.id = c.target))
        """)).scalar()
        componentes = conn.execute(text("""
            SELECT count(DISTINCT component) FROM pgr_connectedComponents(
                'SELECT id, source, target, longitud_m AS cost, -1::float8 AS reverse_cost
                 FROM dim_calles_osm WHERE activo')
        """)).scalar()
        resumen = conn.execute(text("""
            SELECT tipo_via, count(*), round(sum(longitud_m)::numeric / 1000, 1),
                   count(nombre), count(maxspeed_kmh)
            FROM dim_calles_osm WHERE activo
            GROUP BY tipo_via ORDER BY 2 DESC
        """)).fetchall()

    nivel = log.info if huerfanas == 0 else log.error
    nivel(f"  → Aristas activas sin vértice: {huerfanas}")
    log.info(f"  → Componentes conexos (no dirigidos) del grafo activo: {componentes}")
    log.info("  Tipo de vía        | Aristas | Km     | Con nombre | Con maxspeed")
    for tipo, n, km, con_nombre, con_vel in resumen:
        log.info(f"  {tipo:<18} | {n:>7,} | {km:>6} | {con_nombre:>10,} | {con_vel:>12,}")


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------
def ejecutar():
    log.info("=== osmnx_loader.py iniciando ===")
    engine = create_engine(DB_URL, pool_pre_ping=True)

    nodos, aristas = descargar_red_vial(zona.bbox_osmnx())
    aristas_reg, nodos_reg = limpiar_red(nodos, aristas, TIPOS_VIA)
    sincronizar_db(aristas_reg, nodos_reg, engine)
    validar_y_resumir(engine)

    log.info("=== osmnx_loader.py finalizado ===")


if __name__ == "__main__":
    ejecutar()
