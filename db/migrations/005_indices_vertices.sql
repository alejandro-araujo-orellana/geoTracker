-- 005 — (P5) Clave primaria e índice espacial para los nodos del grafo.
-- Sin ellos, encontrar el nodo más cercano a una coordenada (origen/destino
-- de una ruta) hace un recorrido completo de la tabla.
--
-- in_edges/out_edges (salida de pgr_extractVertices) se eliminan: se derivan
-- de dim_calles_osm.source/target y osmnx_loader.py ya no los mantiene.

ALTER TABLE dim_calles_osm_vertices_pgr
    DROP COLUMN IF EXISTS in_edges,
    DROP COLUMN IF EXISTS out_edges;

ALTER TABLE dim_calles_osm_vertices_pgr
    ALTER COLUMN geom TYPE geometry(Point, 4326) USING ST_SetSRID(geom, 4326);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'dim_calles_osm_vertices_pgr'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE dim_calles_osm_vertices_pgr
            ADD CONSTRAINT dim_calles_osm_vertices_pgr_pkey PRIMARY KEY (id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_vertices_geom
    ON dim_calles_osm_vertices_pgr USING gist (geom);
