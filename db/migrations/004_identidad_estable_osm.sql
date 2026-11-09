-- 004 — (P3/P4) Identidad estable y atributos de ruteo en dim_calles_osm.
--
-- (u, v, k) es la clave natural de una arista del grafo de osmnx:
--   u = nodo OSM inicial, v = nodo OSM final, k = índice de arista paralela.
-- osmnx_loader.py hace upsert sobre esta clave, así que re-ejecutarlo ya no
-- duplica la red. source/target pasan a ser los IDs de nodo OSM (u/v).
--
-- Las filas cargadas antes de esta migración tienen u/v/k NULL; la siguiente
-- ejecución de osmnx_loader.py las "adopta" comparando geometrías y conserva
-- su osm_id, por lo que el histórico de trafico_calles no se pierde.

ALTER TABLE dim_calles_osm
    ADD COLUMN IF NOT EXISTS u            bigint,
    ADD COLUMN IF NOT EXISTS v            bigint,
    ADD COLUMN IF NOT EXISTS k            integer,
    ADD COLUMN IF NOT EXISTS maxspeed_kmh double precision,
    ADD COLUMN IF NOT EXISTS oneway       boolean,
    ADD COLUMN IF NOT EXISTS activo       boolean NOT NULL DEFAULT TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_calles_osm_uvk ON dim_calles_osm (u, v, k);
CREATE INDEX IF NOT EXISTS idx_calles_osm_activo ON dim_calles_osm (activo);

-- Una versión anterior del loader guardó NaN de pandas como el texto 'NaN'.
UPDATE dim_calles_osm SET nombre = NULL WHERE nombre = 'NaN';
