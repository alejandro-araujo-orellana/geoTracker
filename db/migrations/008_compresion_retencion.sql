-- 008 — (P8) Compresión, retención y tamaño de chunk de las hypertables.
--
-- Compresión (no borra datos): chunks con más de 7 días. Las escrituras del
-- sistema siempre caen en el chunk actual (lecturas con now(); inferencias
-- con la lectura más reciente), así que no se escribe sobre chunks comprimidos.
-- TimescaleDB exige que las columnas de las restricciones únicas estén en
-- segmentby/orderby: (id_segmento, timestamp_lectura) y (osm_id, timestamp_calculo).
--
-- Retención:
--   fact_lecturas_trafico → SIN retención: son los datos crudos de TomTom y la
--                           base para costos por hora del día (docs/ROUTING.md).
--   trafico_calles        → 365 días: es derivada (IDW) y la más voluminosa.
--
-- Chunk de trafico_calles: 1 día (antes 7). Con ~68 k calles por ciclo cada
-- 15 min son ~6.5 M filas/día; chunks diarios se comprimen y expiran con
-- granularidad fina. Solo afecta a chunks nuevos.

DO $$
BEGIN
    IF NOT (SELECT compression_enabled FROM timescaledb_information.hypertables
            WHERE hypertable_name = 'fact_lecturas_trafico') THEN
        ALTER TABLE fact_lecturas_trafico SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'id_segmento',
            timescaledb.compress_orderby   = 'timestamp_lectura DESC'
        );
    END IF;

    IF NOT (SELECT compression_enabled FROM timescaledb_information.hypertables
            WHERE hypertable_name = 'trafico_calles') THEN
        ALTER TABLE trafico_calles SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'osm_id',
            timescaledb.compress_orderby   = 'timestamp_calculo DESC'
        );
    END IF;
END $$;

SELECT add_compression_policy('fact_lecturas_trafico', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('trafico_calles',        INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('trafico_calles', INTERVAL '365 days', if_not_exists => TRUE);
SELECT set_chunk_time_interval('trafico_calles', INTERVAL '1 day');
