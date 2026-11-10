-- 007 — (P7) Eliminar índice duplicado en trafico_calles.
--
-- idx_trafico_calles_tiempo (creado a mano) era idéntico a
-- trafico_calles_timestamp_calculo_idx, que TimescaleDB crea por defecto en
-- create_hypertable (btree timestamp_calculo DESC). Se conserva el de
-- TimescaleDB porque también existe en una BD creada desde cero con 002.
-- En una hypertable, DROP INDEX se propaga a todos los chunks.

DROP INDEX IF EXISTS idx_trafico_calles_tiempo;
