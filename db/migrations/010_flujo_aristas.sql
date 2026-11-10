-- 010 — Lecturas de tráfico por arista OSM desde vector tiles (D8, D9).
--
-- Una fila por arista medida y por pasada de tiles. La geometría de los
-- tiles NO se guarda (es estable y voluminosa): solo el resultado de
-- asociarla a dim_calles_osm.
--   nivel_relativo = traffic_level de TomTom = velocidad actual / libre (0..1)
--                    → nivel_congestion equivalente = 1 / nivel_relativo
--   cerrada        = road_closure del tile
--
-- Volumen: ~15 k filas por pasada (~1.4 M/día cada 15 min). Chunks diarios,
-- compresión a los 7 días y SIN retención: son datos crudos medidos (D5).

CREATE TABLE IF NOT EXISTS fact_flujo_aristas (
    timestamp_lectura timestamptz      NOT NULL,
    osm_id            bigint           NOT NULL REFERENCES dim_calles_osm (osm_id),
    nivel_relativo    double precision NOT NULL CHECK (nivel_relativo BETWEEN 0 AND 1),
    cerrada           boolean          NOT NULL DEFAULT FALSE,
    CONSTRAINT uq_flujo_arista UNIQUE (timestamp_lectura, osm_id)
);

SELECT create_hypertable('fact_flujo_aristas', 'timestamp_lectura',
                         chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

DO $$
BEGIN
    IF NOT (SELECT compression_enabled FROM timescaledb_information.hypertables
            WHERE hypertable_name = 'fact_flujo_aristas') THEN
        ALTER TABLE fact_flujo_aristas SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'osm_id',
            timescaledb.compress_orderby   = 'timestamp_lectura DESC'
        );
    END IF;
END $$;

SELECT add_compression_policy('fact_flujo_aristas', INTERVAL '7 days', if_not_exists => TRUE);
