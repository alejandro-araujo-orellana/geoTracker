-- 014 — Resumen por ciclo de las aristas medidas por tiles (D38).
--
-- Serie de tiempo del panel 1 de Grafana: congestión promedio y aristas congestionadas/cerradas
-- por ciclo de 15 min. Agregar trafico_calles en cada carga del panel cuesta ~8 s con 90 ciclos
-- (67 k filas por ciclo) y crece lineal con el histórico; el continuous aggregate lo deja
-- precalculado.
--
-- Solo aristas medidas (metodo_inferencia = 'TILE'): las inferidas por IDW suavizan la señal.
-- Umbrales iguales al panel 2 (D37): >= 1.3 congestionada; >= 9.99 cerrada (el predictor
-- asigna 10 a road_closure).
--
-- WITH NO DATA: permite crearlo dentro de la transacción de migrate.py (el refresco inicial
-- no). La política lo materializa en su primera corrida. materialized_only = false: el
-- último ciclo, aún sin materializar, se calcula en vivo.
--
-- Ventana de refresco de 7 días = compress_after de trafico_calles (008): lo más viejo ya no
-- cambia. Si se restaura un histórico más antiguo, refrescarlo a mano (fuera de transacción):
--   CALL refresh_continuous_aggregate('resumen_trafico_ciclo', NULL, now() - INTERVAL '7 days');

CREATE MATERIALIZED VIEW IF NOT EXISTS resumen_trafico_ciclo
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('15 minutes', timestamp_calculo)                AS ciclo,
       count(*)                                                    AS aristas_medidas,
       avg(nivel_congestion_inf)                                   AS congestion_promedio,
       count(*) FILTER (WHERE nivel_congestion_inf >= 1.3)         AS aristas_congestionadas,
       count(*) FILTER (WHERE nivel_congestion_inf >= 9.99)        AS aristas_cerradas
FROM trafico_calles
WHERE metodo_inferencia = 'TILE'
GROUP BY 1
WITH NO DATA;

SELECT add_continuous_aggregate_policy('resumen_trafico_ciclo',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '15 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists     => TRUE);

-- Los privilegios por defecto de 013 ya cubren vistas nuevas; explícito por claridad.
GRANT SELECT ON resumen_trafico_ciclo TO grafana_ro;
