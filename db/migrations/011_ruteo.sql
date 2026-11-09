-- 011 — Ruteo con tráfico real (docs/ROUTING.md, D14).
--
-- costo(arista) = segundos de recorrido con el tráfico actual
--               = longitud_m / velocidad_libre(m/s) × congestión
--   velocidad_libre: maxspeed de OSM si es razonable (10–130 km/h); si no,
--                    la de su tipo de vía (dim_velocidad_tipo_via); si el tipo
--                    no está en la tabla, VELOCIDAD_DEFECTO (20 km/h).
--   congestión:      trafico_calles del último ciclo de los últimos 30 min;
--                    1.0 (flujo libre) si no hay ciclo reciente.
-- Grafo DIRIGIDO: reverse_cost = -1 (osmnx ya duplica las calles de doble
-- sentido como dos aristas). Se excluyen aristas inactivas, auto-lazos y las
-- vías CERRADAS en la última pasada de tiles (intransitables, no solo lentas).

-- ---------------------------------------------------------------------------
-- Velocidad en flujo libre por tipo de vía (km/h). Ajustable con UPDATE;
-- a futuro, calibrar con freeFlowSpeed de Flow Segment Data.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_velocidad_tipo_via (
    tipo_via      text PRIMARY KEY,
    velocidad_kmh double precision NOT NULL CHECK (velocidad_kmh > 0)
);

INSERT INTO dim_velocidad_tipo_via (tipo_via, velocidad_kmh) VALUES
    ('motorway', 80), ('motorway_link', 50),
    ('trunk', 70),    ('trunk_link', 40),
    ('primary', 50),  ('primary_link', 35),
    ('secondary', 45), ('secondary_link', 30),
    ('tertiary', 40), ('tertiary_link', 30),
    ('unclassified', 30),
    ('residential', 25)
ON CONFLICT (tipo_via) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Costos del grafo con el tráfico actual
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_costos_grafo AS
WITH ultimo_ciclo AS (
    SELECT max(timestamp_calculo) AS ts
    FROM trafico_calles
    WHERE timestamp_calculo > now() - interval '30 minutes'
),
ultima_pasada AS (
    SELECT max(timestamp_lectura) AS ts
    FROM fact_flujo_aristas
    WHERE timestamp_lectura > now() - interval '30 minutes'
),
cerradas AS (
    SELECT f.osm_id
    FROM fact_flujo_aristas f
    JOIN ultima_pasada p ON f.timestamp_lectura = p.ts
    WHERE f.cerrada
)
SELECT
    c.id,
    c.source,
    c.target,
    c.longitud_m
        / (COALESCE(
               CASE WHEN c.maxspeed_kmh BETWEEN 10 AND 130 THEN c.maxspeed_kmh END,
               v.velocidad_kmh,
               20.0) / 3.6)
        * COALESCE(t.nivel_congestion_inf, 1.0)  AS cost,
    -1::double precision                          AS reverse_cost
FROM dim_calles_osm c
LEFT JOIN dim_velocidad_tipo_via v USING (tipo_via)
LEFT JOIN trafico_calles t
       ON t.osm_id = c.osm_id
      AND t.timestamp_calculo = (SELECT ts FROM ultimo_ciclo)
WHERE c.activo
  AND c.source <> c.target
  AND c.longitud_m > 0
  AND NOT EXISTS (SELECT 1 FROM cerradas x WHERE x.osm_id = c.osm_id);

-- ---------------------------------------------------------------------------
-- Ruta más rápida entre dos coordenadas (WGS84)
--   Encaje: nodo del grafo más cercano a cada coordenada (índice GiST).
--   Devuelve una fila por arista en orden; vacío si no hay camino
--   (p. ej. el destino solo es alcanzable a contrasentido).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ruta_mas_rapida(
    lat_origen double precision, lon_origen double precision,
    lat_destino double precision, lon_destino double precision
)
RETURNS TABLE (
    seq integer, osm_id bigint, nombre text, tipo_via text, longitud_m double precision,
    segundos double precision, segundos_acumulados double precision, geom geometry
)
LANGUAGE sql STABLE AS $$
    WITH origen AS (
        SELECT id FROM dim_calles_osm_vertices_pgr
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(lon_origen, lat_origen), 4326) LIMIT 1
    ),
    destino AS (
        SELECT id FROM dim_calles_osm_vertices_pgr
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(lon_destino, lat_destino), 4326) LIMIT 1
    )
    SELECT r.seq, c.osm_id, c.nombre, c.tipo_via, c.longitud_m,
           r.cost, r.agg_cost + r.cost, c.geom
    FROM pgr_dijkstra(
             'SELECT id, source, target, cost, reverse_cost FROM v_costos_grafo',
             (SELECT id FROM origen), (SELECT id FROM destino), directed => true) r
    JOIN dim_calles_osm c ON c.id = r.edge
    ORDER BY r.seq;
$$;
