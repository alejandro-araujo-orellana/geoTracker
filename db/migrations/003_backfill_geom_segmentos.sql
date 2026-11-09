-- 003 — (P2) Rellenar la geometría PostGIS de los segmentos TomTom que solo
-- tienen el JSONB crudo. A partir de aquí loader.py escribe geom directamente.
-- Idempotente: solo toca filas con geom NULL y al menos 2 puntos.

UPDATE dim_segmentos_viales d
SET geom = (
    SELECT ST_SetSRID(
               ST_MakeLine(
                   ST_MakePoint((p.punto ->> 'longitude')::double precision,
                                (p.punto ->> 'latitude')::double precision)
                   ORDER BY p.orden),
               4326)
    FROM jsonb_array_elements(d.coordenadas_geometria) WITH ORDINALITY AS p(punto, orden)
)
WHERE d.geom IS NULL
  AND jsonb_typeof(d.coordenadas_geometria) = 'array'
  AND jsonb_array_length(d.coordenadas_geometria) >= 2;
