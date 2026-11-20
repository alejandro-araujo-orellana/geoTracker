-- Fase 4 (D43): fusiona las tablas de paso fase4.* (histórico de Compose) en las tablas del cluster.
-- Lo invoca migrar_historico_compose.sh. Todo o nada: cualquier descuadre aborta la transacción.

BEGIN;

-- osm_id de Compose → osm_id del cluster por la clave natural (u, v, k) (migración 004).
CREATE TEMP TABLE mapa ON COMMIT DROP AS
SELECT r.osm_id AS id_compose, c.osm_id AS id_cluster
FROM fase4.red r
JOIN dim_calles_osm c USING (u, v, k);
CREATE UNIQUE INDEX ON mapa (id_compose);

DO $$
DECLARE
    n_red   bigint := (SELECT count(*) FROM fase4.red);
    n_mapa  bigint := (SELECT count(*) FROM mapa);
    sin_map bigint;
BEGIN
    IF n_mapa <> n_red THEN
        RAISE EXCEPTION 'Red: % aristas de Compose y solo % con (u,v,k) en el cluster', n_red, n_mapa;
    END IF;
    SELECT count(*) INTO sin_map FROM (
        SELECT osm_id FROM fase4.trafico_calles UNION SELECT osm_id FROM fase4.flujo
    ) h LEFT JOIN mapa m ON m.id_compose = h.osm_id WHERE m.id_compose IS NULL;
    IF sin_map > 0 THEN
        RAISE EXCEPTION '% osm_id del histórico sin equivalente en el cluster', sin_map;
    END IF;
    RAISE NOTICE 'Mapa completo: % aristas (% con osm_id distinto)',
        n_mapa, (SELECT count(*) FROM mapa WHERE id_compose <> id_cluster);
END $$;

-- Segmentos TomTom: Compose tiene los 440 originales (+ históricos); el cluster, los 207 deduplicados.
-- Los que faltan solo dan nombre/geometría a lecturas antiguas; el predictor usa los de lecturas recientes.
INSERT INTO dim_segmentos_viales (id_segmento, nombre_via, coordenadas_geometria, geom)
SELECT id_segmento, nombre_via, coordenadas_geometria, geom FROM fase4.segmentos
ON CONFLICT (id_segmento) DO NOTHING;

-- Hechos: sin ON CONFLICT a propósito. El rango es anterior al corte y el cluster no tiene nada ahí
-- (lo comprueba el script); un choque significaría un supuesto roto y debe abortar.
INSERT INTO fact_lecturas_trafico (timestamp_lectura, id_segmento, velocidad_actual, tiempo_recorrido, nivel_congestion)
SELECT timestamp_lectura, id_segmento, velocidad_actual, tiempo_recorrido, nivel_congestion
FROM fase4.lecturas;

INSERT INTO fact_flujo_aristas (timestamp_lectura, osm_id, nivel_relativo, cerrada)
SELECT f.timestamp_lectura, m.id_cluster, f.nivel_relativo, f.cerrada
FROM fase4.flujo f JOIN mapa m ON m.id_compose = f.osm_id;

-- id (bigserial) no se copia: lo asigna la secuencia del cluster.
INSERT INTO trafico_calles (timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia)
SELECT t.timestamp_calculo, m.id_cluster, t.nivel_congestion_inf, t.metodo_inferencia
FROM fase4.trafico_calles t JOIN mapa m ON m.id_compose = t.osm_id;

DO $$
DECLARE
    corte timestamptz := (SELECT min(timestamp_calculo) FROM fase4.trafico_calles);  -- solo para el mensaje
    a bigint; b bigint;
BEGIN
    a := (SELECT count(*) FROM fase4.trafico_calles);
    b := (SELECT count(*) FROM trafico_calles WHERE timestamp_calculo <= (SELECT max(timestamp_calculo) FROM fase4.trafico_calles));
    IF a <> b THEN RAISE EXCEPTION 'trafico_calles: % de paso, % en destino', a, b; END IF;
    a := (SELECT count(*) FROM fase4.flujo);
    b := (SELECT count(*) FROM fact_flujo_aristas WHERE timestamp_lectura <= (SELECT max(timestamp_lectura) FROM fase4.flujo));
    IF a <> b THEN RAISE EXCEPTION 'fact_flujo_aristas: % de paso, % en destino', a, b; END IF;
    a := (SELECT count(*) FROM fase4.lecturas);
    b := (SELECT count(*) FROM fact_lecturas_trafico WHERE timestamp_lectura <= (SELECT max(timestamp_lectura) FROM fase4.lecturas));
    IF a <> b THEN RAISE EXCEPTION 'fact_lecturas_trafico: % de paso, % en destino', a, b; END IF;
    RAISE NOTICE 'Conteos cuadran (histórico desde %)', corte;
END $$;

COMMIT;
