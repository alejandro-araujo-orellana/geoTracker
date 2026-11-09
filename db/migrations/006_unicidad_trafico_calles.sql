-- 006 — (P6) Idempotencia de trafico_calles.
--
-- Una inferencia por calle y por estado del tráfico. predictor.py usa como
-- timestamp_calculo la lectura TomTom más reciente en la que se basa el
-- cálculo, así que re-ejecutarlo sobre los mismos datos no duplica filas
-- (INSERT ... ON CONFLICT ON CONSTRAINT uq_trafico_calles_ciclo DO NOTHING).
--
-- La restricción incluye timestamp_calculo porque TimescaleDB exige que
-- toda restricción única de una hypertable contenga la columna de partición.
-- Falla (y no se marca aplicada) si ya existen duplicados: hay que limpiarlos antes.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'trafico_calles'::regclass AND conname = 'uq_trafico_calles_ciclo'
    ) THEN
        ALTER TABLE trafico_calles
            ADD CONSTRAINT uq_trafico_calles_ciclo UNIQUE (timestamp_calculo, osm_id);
    END IF;
END $$;
