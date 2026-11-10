-- 002 — Esquema base (línea base del esquema que existía creado a mano).
-- Idempotente: en la BD existente no cambia nada; en una BD vacía la crea completa.
-- La tabla legado trafico_inferido NO se recrea (ver docs/DATABASE.md).

-- ---------------------------------------------------------------------------
-- Segmentos medidos por TomTom
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_segmentos_viales (
    id_segmento           text PRIMARY KEY,
    nombre_via            text NOT NULL,
    coordenadas_geometria jsonb,
    geom                  geometry(LineString, 4326)
);
ALTER TABLE dim_segmentos_viales
    ADD COLUMN IF NOT EXISTS geom geometry(LineString, 4326);
CREATE INDEX IF NOT EXISTS idx_segmentos_geom
    ON dim_segmentos_viales USING gist (geom);

-- ---------------------------------------------------------------------------
-- Lecturas TomTom (hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_lecturas_trafico (
    timestamp_lectura timestamptz NOT NULL,
    id_segmento       text        NOT NULL,
    velocidad_actual  double precision,
    tiempo_recorrido  double precision,
    nivel_congestion  double precision,
    CONSTRAINT uq_lectura_por_calle UNIQUE (timestamp_lectura, id_segmento)
);
SELECT create_hypertable('fact_lecturas_trafico', 'timestamp_lectura', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Red vial OSM (aristas del grafo de ruteo)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_calles_osm (
    osm_id      bigint PRIMARY KEY,
    id          bigserial NOT NULL,
    nombre      text,
    tipo_via    text NOT NULL,
    longitud_m  double precision,
    geom        geometry(LineString, 4326),
    centroide_m geometry(Point, 32614),
    source      bigint,
    target      bigint
);
CREATE INDEX IF NOT EXISTS idx_calles_osm_geom      ON dim_calles_osm USING gist (geom);
CREATE INDEX IF NOT EXISTS idx_calles_osm_centroide ON dim_calles_osm USING gist (centroide_m);
CREATE INDEX IF NOT EXISTS idx_calles_osm_tipo      ON dim_calles_osm (tipo_via);
CREATE INDEX IF NOT EXISTS idx_calles_source        ON dim_calles_osm (source);
CREATE INDEX IF NOT EXISTS idx_calles_target        ON dim_calles_osm (target);

-- ---------------------------------------------------------------------------
-- Nodos del grafo (intersecciones). Índices en 005.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_calles_osm_vertices_pgr (
    id   bigint,
    x    double precision,
    y    double precision,
    geom geometry
);

-- ---------------------------------------------------------------------------
-- Congestión inferida por calle OSM (hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trafico_calles (
    id                   bigserial,
    timestamp_calculo    timestamptz      NOT NULL,
    osm_id               bigint           NOT NULL REFERENCES dim_calles_osm (osm_id),
    nivel_congestion_inf double precision NOT NULL,
    metodo_inferencia    text DEFAULT 'IDW_OSM'
);
SELECT create_hypertable('trafico_calles', 'timestamp_calculo', if_not_exists => TRUE);
