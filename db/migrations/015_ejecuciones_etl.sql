-- 015 — Una fila por ciclo del ETL (D31, D41).
--
-- kube-state-metrics dice si el Job del CronJob terminó; esta tabla dice QUÉ hizo el ciclo:
-- un Job "exitoso" puede no haber medido nada (tiles caídos o bloqueados, sin presupuesto).
-- La escribe src/ejecuciones.py al final de cada ciclo (también si falla). Sobrevive a la
-- retención de Prometheus (7 días) y se grafica/alerta desde Grafana con grafana_ro.
--
-- Tabla normal, no hypertable: ~96 filas/día (~35 k/año).
--
-- estado: ok        = hubo datos y corrió el predictor
--         sin_datos = ninguna fuente trajo datos; no corrió el predictor
--         error     = el ciclo lanzó una excepción (el Job sale con 1)
-- error: texto de la(s) excepción(es), también las que el ciclo absorbe (fallo de tiles con
--        Segment Data como respaldo: estado ok, error anotado).

CREATE TABLE IF NOT EXISTS ejecuciones_etl (
    inicio           timestamptz PRIMARY KEY,
    fin              timestamptz NOT NULL,
    duracion_s       double precision GENERATED ALWAYS AS (EXTRACT(EPOCH FROM (fin - inicio))) STORED,
    estado           text    NOT NULL CONSTRAINT ejecuciones_etl_estado_check
                             CHECK (estado IN ('ok', 'sin_datos', 'error')),
    aristas_medidas  integer NOT NULL DEFAULT 0,
    tiles_peticiones integer NOT NULL DEFAULT 0,
    tiles_bloqueado  boolean NOT NULL DEFAULT FALSE,
    sd_peticiones    integer NOT NULL DEFAULT 0,
    sd_bloqueado     boolean NOT NULL DEFAULT FALSE,
    sd_cargado       boolean NOT NULL DEFAULT FALSE,
    predictor        boolean NOT NULL DEFAULT FALSE,
    error            text,
    pod              text
);

-- Los privilegios por defecto de 013 ya la cubren; explícito por claridad.
GRANT SELECT ON ejecuciones_etl TO grafana_ro;
