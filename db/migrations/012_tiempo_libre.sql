-- 012 — Tiempo a flujo libre por arista como vista propia (D16).
--
-- La API necesita, para cada ruta, el tiempo con tráfico (v_costos_grafo) y
-- el tiempo a flujo libre (para reportar la demora por congestión). Para no
-- duplicar la fórmula de velocidad libre, se extrae a v_tiempo_libre y
-- v_costos_grafo pasa a usarla: una sola fuente de verdad.
-- La lógica es idéntica a la de 011 (mismas columnas y resultados).

CREATE OR REPLACE VIEW v_tiempo_libre AS
SELECT
    c.osm_id,
    c.id,
    c.longitud_m
        / (COALESCE(
               CASE WHEN c.maxspeed_kmh BETWEEN 10 AND 130 THEN c.maxspeed_kmh END,
               v.velocidad_kmh,
               20.0) / 3.6) AS segundos_libre
FROM dim_calles_osm c
LEFT JOIN dim_velocidad_tipo_via v USING (tipo_via);

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
    l.segundos_libre * COALESCE(t.nivel_congestion_inf, 1.0) AS cost,
    -1::double precision                                    AS reverse_cost
FROM dim_calles_osm c
JOIN v_tiempo_libre l ON l.osm_id = c.osm_id
LEFT JOIN trafico_calles t
       ON t.osm_id = c.osm_id
      AND t.timestamp_calculo = (SELECT ts FROM ultimo_ciclo)
WHERE c.activo
  AND c.source <> c.target
  AND c.longitud_m > 0
  AND NOT EXISTS (SELECT 1 FROM cerradas x WHERE x.osm_id = c.osm_id);
