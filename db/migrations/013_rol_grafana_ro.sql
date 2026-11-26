-- 013 — Rol de solo lectura para Grafana (D33).
--
-- Grafana consulta la BD con este rol, no con el dueño del esquema: un dashboard
-- (o quien entre a Grafana) no puede modificar ni borrar datos.
--
-- La migración crea el rol SIN login y sin contraseña: la contraseña no puede vivir
-- en git. migrate.py la asigna (y habilita el login) si recibe GRAFANA_DB_PASSWORD;
-- en Kubernetes viene del Secret. Los roles son globales al servidor, no a la BD.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO grafana_ro;

-- Tablas existentes. En las hypertables, TimescaleDB propaga el permiso a los chunks.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;

-- Tablas que creen migraciones futuras (las aplica este mismo rol dueño): sin GRANT a mano.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;

-- Un panel pesado no puede dejar consultas corriendo ni acaparar conexiones.
ALTER ROLE grafana_ro SET statement_timeout = '30s';
ALTER ROLE grafana_ro CONNECTION LIMIT 10;
