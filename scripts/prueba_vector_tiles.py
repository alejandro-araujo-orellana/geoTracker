"""
prueba_vector_tiles.py — PRUEBA (opción A, docs/ROUTING.md)
==========================================================
¿Sirven los Traffic Flow *vector tiles* de TomTom como fuente principal?
Cuota: 200 000 tiles/mes vs 20 000 peticiones/mes de Flow Segment Data.

Qué hace (no modifica el esquema; todo en tablas TEMPORALES):
  1. Descarga los tiles 'relative' que cubren la zona de estudio (zona.py).
  2. Decodifica los tramos (traffic_level = velocidad actual / velocidad libre).
  3. Los asocia a las aristas activas de dim_calles_osm: tramo más cercano a
     menos de TOLERANCIA_M del punto medio de la arista y con rumbo parecido.
  4. Mide cobertura por tipo de vía y compara contra las lecturas frescas
     de Flow Segment Data (traffic_level vs 1 / nivel_congestion).
  5. Registra los tiles consumidos en consumo_api.

Uso (desde la raíz; requiere `pip install mapbox-vector-tile==2.1.0`):
    python scripts/prueba_vector_tiles.py [zoom]
"""

import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mapbox_vector_tile
import requests
from sqlalchemy import create_engine, text

import config

sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))
import presupuesto  # noqa: E402
import zona  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

URL_TILE = "https://api.tomtom.com/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.pbf"
TAGS = "[road_type,traffic_level,traffic_road_coverage,road_closure,road_category,road_subcategory]"
API_TILES = "tomtom_flow_vector_tiles"
TOLERANCIA_M = 12
TOLERANCIA_RUMBO_GRADOS = 35
VENTANA_SEGMENT_DATA_MIN = 90


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------
def tile_de(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tiles_de_la_zona(z: int) -> list[tuple[int, int]]:
    x0, y0 = tile_de(zona.LAT_MAX, zona.LON_MIN, z)  # noroeste
    x1, y1 = tile_de(zona.LAT_MIN, zona.LON_MAX, z)  # sureste
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def a_lonlat(z: int, x: int, y: int, px: float, py: float, extent: int) -> tuple[float, float]:
    n = 2 ** z
    gx, gy = x + px / extent, y + py / extent  # py medido desde ARRIBA (y_coord_down)
    lon = gx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / n))))
    return lon, lat


def descargar_tramos(z: int) -> tuple[list[dict], int]:
    tramos, peticiones = [], 0
    for x, y in tiles_de_la_zona(z):
        r = requests.get(URL_TILE.format(z=z, x=x, y=y),
                         params={"key": config.TOMTOM_API_KEY, "tags": TAGS}, timeout=15)
        peticiones += 1
        if r.status_code != 200:
            log.warning(f"tile {z}/{x}/{y}: HTTP {r.status_code}")
            continue
        capa = mapbox_vector_tile.decode(r.content, default_options={"y_coord_down": True}).get("Traffic flow")
        if not capa:
            continue
        extent = capa.get("extent", 4096)
        for f in capa["features"]:
            g = f["geometry"]
            lineas = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
            for linea in lineas:
                if len(linea) < 2:
                    continue
                coords = ", ".join(f"{lon!r} {lat!r}" for lon, lat in
                                   (a_lonlat(z, x, y, px, py, extent) for px, py in linea))
                p = f["properties"]
                tramos.append({
                    "nivel": p.get("traffic_level"),
                    "categoria": p.get("road_category"),
                    "subcategoria": p.get("road_subcategory"),
                    "cerrada": bool(p.get("road_closure", False)),
                    "wkt": f"LINESTRING({coords})",
                })
        time.sleep(0.05)
    return tramos, peticiones


# ---------------------------------------------------------------------------
# Análisis en PostGIS (tablas temporales)
# ---------------------------------------------------------------------------
SQL_CARGA = """
CREATE TEMP TABLE tramos_tile (
    id serial, nivel float8, categoria text, subcategoria text, cerrada boolean, wkt text
) ON COMMIT DROP;
"""

SQL_PREPARAR = """
ALTER TABLE tramos_tile ADD COLUMN geom_m geometry(LineString, 32614);
UPDATE tramos_tile SET geom_m = ST_Transform(ST_SetSRID(ST_GeomFromText(wkt), 4326), 32614);
CREATE INDEX ON tramos_tile USING gist (geom_m);
ANALYZE tramos_tile;

CREATE TEMP TABLE asociacion ON COMMIT DROP AS
SELECT c.osm_id, c.tipo_via, c.longitud_m, t.nivel, t.cerrada, t.categoria
FROM dim_calles_osm c
CROSS JOIN LATERAL (
    SELECT t.*
    FROM tramos_tile t
    WHERE ST_DWithin(t.geom_m, ST_LineInterpolatePoint(ST_Transform(c.geom, 32614), 0.5), :tol)
      AND (
        -- diferencia de rumbo sin sentido (mod 180) entre la arista y el tramo
        abs(mod((degrees(ST_Azimuth(ST_StartPoint(ST_Transform(c.geom, 32614)),
                                    ST_EndPoint(ST_Transform(c.geom, 32614))))
               - degrees(ST_Azimuth(ST_StartPoint(t.geom_m), ST_EndPoint(t.geom_m))) + 360)::numeric, 180)) < :rumbo
        OR abs(mod((degrees(ST_Azimuth(ST_StartPoint(ST_Transform(c.geom, 32614)),
                                       ST_EndPoint(ST_Transform(c.geom, 32614))))
               - degrees(ST_Azimuth(ST_StartPoint(t.geom_m), ST_EndPoint(t.geom_m))) + 360)::numeric, 180)) > 180 - :rumbo
      )
    ORDER BY t.geom_m <-> ST_LineInterpolatePoint(ST_Transform(c.geom, 32614), 0.5)
    LIMIT 1
) t
WHERE c.activo AND ST_NPoints(c.geom) >= 2 AND c.source <> c.target;
"""

SQL_COBERTURA = """
SELECT c.tipo_via,
       count(*)                                   AS aristas,
       count(a.osm_id)                            AS con_tile,
       round(100.0 * count(a.osm_id) / count(*), 1) AS pct_aristas,
       round((100.0 * coalesce(sum(a.longitud_m), 0) / sum(c.longitud_m))::numeric, 1) AS pct_km
FROM dim_calles_osm c
LEFT JOIN asociacion a USING (osm_id)
WHERE c.activo AND c.source <> c.target
GROUP BY ROLLUP (c.tipo_via)
ORDER BY count(*) DESC
"""

SQL_COMPARACION = """
WITH frescas AS (
    SELECT DISTINCT ON (f.id_segmento) f.id_segmento, f.nivel_congestion, f.timestamp_lectura,
           ST_Transform(d.geom, 32614) AS geom_m
    FROM fact_lecturas_trafico f
    JOIN dim_segmentos_viales d USING (id_segmento)
    WHERE f.timestamp_lectura > now() - make_interval(mins => :ventana)
      AND f.nivel_congestion > 0
    ORDER BY f.id_segmento, f.timestamp_lectura DESC
)
SELECT fr.id_segmento,
       round((1 / fr.nivel_congestion)::numeric, 3) AS relativa_segment_data,
       round((sum(t.nivel * ST_Length(t.geom_m)) / nullif(sum(ST_Length(t.geom_m)), 0))::numeric, 3) AS relativa_tiles,
       count(t.id) AS tramos
FROM frescas fr
LEFT JOIN tramos_tile t
       ON ST_DWithin(t.geom_m, fr.geom_m, :tol) AND NOT t.cerrada
      AND ST_Length(ST_Intersection(ST_Buffer(fr.geom_m, :tol), t.geom_m)) > 0.5 * ST_Length(t.geom_m)
GROUP BY fr.id_segmento, fr.nivel_congestion
ORDER BY fr.id_segmento
"""


def analizar(engine, tramos: list[dict]) -> None:
    with engine.begin() as conn:
        conn.execute(text(SQL_CARGA))
        conn.execute(text("INSERT INTO tramos_tile (nivel, categoria, subcategoria, cerrada, wkt) "
                          "VALUES (:nivel, :categoria, :subcategoria, :cerrada, :wkt)"), tramos)
        conn.execute(text(SQL_PREPARAR), {"tol": TOLERANCIA_M, "rumbo": TOLERANCIA_RUMBO_GRADOS})

        log.info("Cobertura de la red OSM activa por los tiles:")
        log.info("  tipo_via          | aristas | con tile | % aristas | % km")
        for tipo, n, con, pct, pct_km in conn.execute(text(SQL_COBERTURA)):
            log.info(f"  {tipo or 'TOTAL':<17} | {n:>7,} | {con:>8,} | {pct:>8}% | {pct_km:>5}%")

        dist = conn.execute(text("""
            SELECT count(*) FILTER (WHERE cerrada) cerradas,
                   round(avg(nivel)::numeric, 3) media,
                   count(*) FILTER (WHERE nivel < 0.6 AND NOT cerrada) lentas
            FROM asociacion""")).one()
        log.info(f"Aristas asociadas: nivel medio {dist.media}, {dist.lentas:,} con nivel < 0.6, "
                 f"{dist.cerradas:,} marcadas como cerradas")

        filas = conn.execute(text(SQL_COMPARACION),
                             {"ventana": VENTANA_SEGMENT_DATA_MIN, "tol": TOLERANCIA_M}).fetchall()
        log.info(f"Comparación con Flow Segment Data (lecturas de los últimos {VENTANA_SEGMENT_DATA_MIN} min):")
        difs = []
        for seg, rel_sd, rel_t, n in filas:
            log.info(f"  {seg:<45} segment_data={rel_sd} tiles={rel_t} ({n} tramos)")
            if rel_t is not None:
                difs.append(abs(float(rel_sd) - float(rel_t)))
        if difs:
            log.info(f"  → {len(difs)}/{len(filas)} comparables | diferencia absoluta media "
                     f"{sum(difs) / len(difs):.3f} | máx {max(difs):.3f}")


def main() -> None:
    z = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    for zz in (13, 14, 15):
        log.info(f"zoom {zz}: {len(tiles_de_la_zona(zz))} tiles por pasada sobre la zona")

    engine = create_engine(config.DB_URL)
    hoy = datetime.now(ZoneInfo("America/Mexico_City")).date()
    tramos, peticiones = [], 0
    try:
        tramos, peticiones = descargar_tramos(z)
    finally:
        presupuesto.registrar_consumo(engine, hoy, peticiones, False, api=API_TILES)
    log.info(f"zoom {z}: {peticiones} tiles descargados → {len(tramos):,} tramos de tráfico")
    analizar(engine, tramos)


if __name__ == "__main__":
    main()
