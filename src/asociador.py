"""
asociador.py
============
Asocia los tramos de los vector tiles (extractor_tiles.py) a las aristas
dirigidas de dim_calles_osm y guarda el resultado en fact_flujo_aristas.

Regla (D9, docs/DECISIONES.md), todo en metros (EPSG:32614):
  Para cada arista activa se toma el tramo de tile más cercano a su punto
  medio que cumpla:
    - distancia < TOLERANCIA_M, y
    - mismo SENTIDO: diferencia de rumbo local < TOLERANCIA_RUMBO_GRADOS
      (sobre 360°, no 180°). Los tiles traen una línea por sentido en vías
      de doble sentido, igual que las aristas dirigidas de OSM.
  El rumbo es LOCAL (alrededor del punto medio de la arista y del punto más
  cercano del tramo), para no confundir calles curvas.

Idempotente: ON CONFLICT (timestamp_lectura, osm_id) DO NOTHING.
"""

import logging
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)

TOLERANCIA_M = 12
TOLERANCIA_RUMBO_GRADOS = 35
VENTANA_RUMBO_M = 10  # longitud a cada lado del punto para medir el rumbo local

_SQL_STAGING = """
CREATE TEMP TABLE tramos_tile (
    id serial, nivel float8, cerrada boolean, categoria text, wkt text
) ON COMMIT DROP;
"""

_SQL_PREPARAR = """
ALTER TABLE tramos_tile ADD COLUMN geom_m geometry(LineString, 32614);
UPDATE tramos_tile SET geom_m = ST_Transform(ST_SetSRID(ST_GeomFromText(wkt), 4326), 32614);
DELETE FROM tramos_tile WHERE geom_m IS NULL OR ST_Length(geom_m) = 0;
CREATE INDEX ON tramos_tile USING gist (geom_m);
ANALYZE tramos_tile;
"""

# diferencia angular mínima entre dos rumbos en grados (0..180)
_DIF = "abs(mod((({a}) - ({b}) + 540)::numeric, 360) - 180)"

# Punto medio y rumbo local de cada arista activa, una sola vez por pasada.
_SQL_ARISTAS = """
CREATE TEMP TABLE aristas_m ON COMMIT DROP AS
SELECT osm_id,
       ST_LineInterpolatePoint(g, 0.5) AS medio,
       degrees(ST_Azimuth(ST_LineInterpolatePoint(g, GREATEST(0.5 - :ventana / ST_Length(g), 0)),
                          ST_LineInterpolatePoint(g, LEAST(0.5 + :ventana / ST_Length(g), 1)))) AS rumbo
FROM (
    SELECT osm_id, ST_Transform(geom, 32614) AS g
    FROM dim_calles_osm
    WHERE activo AND source <> target
) c
WHERE ST_Length(g) > 0;
DELETE FROM aristas_m WHERE rumbo IS NULL;
-- Imprescindible: sin índice y estadísticas en esta tabla temporal, PostgreSQL
-- hacía el producto cruzado completo (67 k aristas × 4 k tramos ≈ 280 M pares,
-- calculando el rumbo de cada uno): >7 min por pasada. Con esto: ~1.3 s.
CREATE INDEX ON aristas_m USING gist (medio);
ANALYZE aristas_m;
"""

# Por cada tramo, el índice de aristas_m da las aristas a < TOLERANCIA_M; solo esos
# pares (~24 k) calculan el rumbo. DISTINCT ON se queda con el tramo más cercano
# de cada arista.
_SQL_ASOCIAR = f"""
INSERT INTO fact_flujo_aristas (timestamp_lectura, osm_id, nivel_relativo, cerrada)
SELECT DISTINCT ON (c.osm_id) :ts, c.osm_id, LEAST(GREATEST(c.nivel, 0), 1), c.cerrada
FROM (
    SELECT a.osm_id, a.rumbo, t.nivel, t.cerrada,
           ST_Distance(t.geom_m, a.medio) AS dist,
           ST_LineLocatePoint(t.geom_m, a.medio) AS f,
           LEAST(:ventana / ST_Length(t.geom_m), 0.5) AS d,
           t.geom_m
    FROM aristas_m a
    JOIN tramos_tile t ON ST_DWithin(t.geom_m, a.medio, :tol)
) c
WHERE {_DIF.format(
    a="degrees(ST_Azimuth(ST_LineInterpolatePoint(c.geom_m, GREATEST(c.f - c.d, 0)), "
      "ST_LineInterpolatePoint(c.geom_m, LEAST(c.f + c.d, 1))))",
    b="c.rumbo")} < :tol_rumbo
ORDER BY c.osm_id, c.dist
ON CONFLICT ON CONSTRAINT uq_flujo_arista DO NOTHING
"""


def asociar_y_guardar(engine, tramos: list[dict], timestamp: datetime) -> int:
    """Guarda en fact_flujo_aristas las aristas medidas por esta pasada. Devuelve cuántas."""
    if not tramos:
        return 0
    with engine.begin() as conn:
        conn.execute(text(_SQL_STAGING))
        conn.execute(
            text("INSERT INTO tramos_tile (nivel, cerrada, categoria, wkt) "
                 "VALUES (:nivel, :cerrada, :categoria, :wkt)"),
            tramos,
        )
        conn.execute(text(_SQL_PREPARAR))
        conn.execute(text(_SQL_ARISTAS), {"ventana": float(VENTANA_RUMBO_M)})
        insertadas = conn.execute(text(_SQL_ASOCIAR), {
            "ts": timestamp, "tol": TOLERANCIA_M, "tol_rumbo": TOLERANCIA_RUMBO_GRADOS,
            "ventana": float(VENTANA_RUMBO_M),
        }).rowcount
    logger.info("Asociación: %d tramos → %d aristas OSM medidas (%s).", len(tramos), insertadas, timestamp.isoformat())
    return insertadas
