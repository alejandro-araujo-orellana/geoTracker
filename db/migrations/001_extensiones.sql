-- 001 — Extensiones requeridas por el sistema.
-- Idempotente: se puede aplicar sobre la BD existente o sobre una vacía.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
