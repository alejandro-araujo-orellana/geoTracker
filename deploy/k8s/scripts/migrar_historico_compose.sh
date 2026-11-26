#!/usr/bin/env bash
# Fase 4 (D43): copia el histórico de la BD de Compose (trafico_db) a la del cluster (geotracker-db-0).
#
# Por qué no pg_dump/pg_restore completo: el cluster ya tiene datos propios desde el corte (D29) y su red OSM
# tiene otros osm_id para 24 574 aristas (Compose conserva osm_id "adoptados"; el cluster, blake2b(u,v,k)).
# El histórico se traduce osm_id(Compose) → (u,v,k) → osm_id(cluster) y se inserta solo lo anterior al corte.
#
# Pasos: respaldo del cluster → extracción de Compose a archivos → tablas de paso (esquema fase4) → fusión en
# una transacción con verificación de conteos → refresco del continuous aggregate → compresión → verificación
# por día contra Compose (conteos y sumas exactas en numeric, ponderadas por u para detectar mal mapeo).
#
# Idempotente: si el cluster ya tiene datos anteriores al corte, salta la fusión y solo repite lo posterior.
# Compose solo se lee. Requiere docker y kubectl con acceso al cluster. Ejecutar desde cualquier carpeta:
#   deploy/k8s/scripts/migrar_historico_compose.sh
set -euo pipefail

CORTE="${CORTE:-2026-11-13 17:15:00+00}"   # primer ciclo del CronJob (D29); Compose termina en 17:02:45
NS="${NS:-geotracker}"
POD="${POD:-geotracker-db-0}"
COMPOSE_CT="${COMPOSE_CT:-trafico_db}"
COMPOSE_USER="${COMPOSE_USER:-user}"
COMPOSE_DB="${COMPOSE_DB:-vialidades_slp}"
DIR="${FASE4_DIR:-/tmp/fase4}"
RESPALDO_DIR="${RESPALDO_DIR:-$HOME/geotracker-respaldos}"
AQUI="$(cd "$(dirname "$0")" && pwd)"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
compose_psql() { docker exec -i "$COMPOSE_CT" psql -X -q -v ON_ERROR_STOP=1 -U "$COMPOSE_USER" -d "$COMPOSE_DB" "$@"; }
k8s_psql() {
  kubectl exec -i -n "$NS" "$POD" -- \
    sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"
}

TABLAS=(red segmentos lecturas flujo trafico_calles)
mkdir -p "$DIR" "$RESPALDO_DIR"

# Reanudar: si una corrida anterior dejó fase4 completo (conteos = archivos), se salta a la fusión.
paso_completo() {
  [[ "$(k8s_psql -Atc "SELECT to_regclass('fase4.trafico_calles') IS NOT NULL")" == "t" ]] || return 1
  local t
  for t in "${TABLAS[@]}"; do
    [[ -f "$DIR/$t.tsv" ]] || return 1
    [[ "$(k8s_psql -Atc "SELECT count(*) FROM fase4.$t")" == "$(wc -l < "$DIR/$t.tsv")" ]] || return 1
  done
}

ya_migrado=$(k8s_psql -Atc "SELECT EXISTS (SELECT 1 FROM trafico_calles WHERE timestamp_calculo < '$CORTE')")

if [[ "$ya_migrado" == "f" ]] && paso_completo; then
  log "Tablas de paso fase4 completas de una corrida anterior: se reanuda en la fusión."
elif [[ "$ya_migrado" == "f" ]]; then
  # 1. Respaldo del cluster antes de tocarlo.
  respaldo="$RESPALDO_DIR/k8s-antes-fase4-$(date -u +%Y%m%dT%H%M%SZ).dump"
  log "Respaldo del cluster → $respaldo"
  kubectl exec -n "$NS" "$POD" -- sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$respaldo" 2> "$DIR/pg_dump.log"
  pg_restore -l "$respaldo" > /dev/null 2>&1 || docker run --rm -i --entrypoint pg_restore \
    geotracker-timescaledb:pg15.2-ts2.10.2 -l < "$respaldo" > /dev/null
  log "  respaldo legible ($(du -h "$respaldo" | cut -f1))"

  # 2. Extracción de Compose (solo lectura) a archivos.
  log "Extrayendo de Compose (< $CORTE)"
  compose_psql -c "COPY (SELECT osm_id, u, v, k FROM dim_calles_osm) TO STDOUT" > "$DIR/red.tsv"
  compose_psql -c "COPY dim_segmentos_viales TO STDOUT" > "$DIR/segmentos.tsv"
  compose_psql -c "COPY (SELECT timestamp_lectura, id_segmento, velocidad_actual, tiempo_recorrido, nivel_congestion
                         FROM fact_lecturas_trafico WHERE timestamp_lectura < '$CORTE') TO STDOUT" > "$DIR/lecturas.tsv"
  compose_psql -c "COPY (SELECT timestamp_lectura, osm_id, nivel_relativo, cerrada
                         FROM fact_flujo_aristas WHERE timestamp_lectura < '$CORTE') TO STDOUT" > "$DIR/flujo.tsv"
  compose_psql -c "COPY (SELECT timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia
                         FROM trafico_calles WHERE timestamp_calculo < '$CORTE') TO STDOUT" > "$DIR/trafico_calles.tsv"
  wc -l "$DIR"/*.tsv >&2

  # 3. Tablas de paso en el cluster (UNLOGGED: no generan WAL; se borran al final).
  log "Cargando tablas de paso (esquema fase4)"
  k8s_psql <<'SQL'
DROP SCHEMA IF EXISTS fase4 CASCADE;
CREATE SCHEMA fase4;
CREATE UNLOGGED TABLE fase4.red            (osm_id bigint, u bigint, v bigint, k integer);
CREATE UNLOGGED TABLE fase4.segmentos      (LIKE public.dim_segmentos_viales);
CREATE UNLOGGED TABLE fase4.lecturas       (timestamp_lectura timestamptz, id_segmento text, velocidad_actual float8,
                                            tiempo_recorrido float8, nivel_congestion float8);
CREATE UNLOGGED TABLE fase4.flujo          (timestamp_lectura timestamptz, osm_id bigint, nivel_relativo float8,
                                            cerrada boolean);
CREATE UNLOGGED TABLE fase4.trafico_calles (timestamp_calculo timestamptz, osm_id bigint, nivel_congestion_inf float8,
                                            metodo_inferencia text);
SQL
  # Comprimido: con 690 MB por stdin, `kubectl exec` se quedó colgado después de que el COPY ya había terminado.
  for t in "${TABLAS[@]}"; do
    gzip -1 -c "$DIR/$t.tsv" | kubectl exec -i -n "$NS" "$POD" -- \
      sh -c 'gunzip -c | psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "COPY fase4.$1 FROM STDIN"' sh "$t"
    log "  fase4.$t cargada"
  done
  paso_completo || { log "ERROR: los conteos de fase4 no coinciden con los archivos extraídos"; exit 1; }
fi

if [[ "$ya_migrado" == "f" ]]; then
  # 4. Fusión: una sola transacción; aborta si algo no cuadra.
  log "Fusionando (una transacción)"
  k8s_psql -f - < "$AQUI/fusionar_historico.sql"
  k8s_psql -c "DROP SCHEMA fase4 CASCADE"
else
  log "El cluster ya tiene datos anteriores a $CORTE: se salta la fusión."
fi

# 5. Continuous aggregate (D38): su política solo refresca 7 días hacia atrás.
log "Refrescando resumen_trafico_ciclo hasta el corte"
k8s_psql -c "CALL refresh_continuous_aggregate('resumen_trafico_ciclo', NULL, '$CORTE')"

# 6. Compresión de lo que ya pasó de 7 días (lo mismo que haría la política 008/010, sin esperarla).
log "Comprimiendo chunks de más de 7 días"
k8s_psql -Atc "
  SELECT count(compress_chunk(c, if_not_compressed => true))
  FROM (SELECT show_chunks('trafico_calles',        older_than => INTERVAL '7 days') UNION ALL
        SELECT show_chunks('fact_flujo_aristas',    older_than => INTERVAL '7 days') UNION ALL
        SELECT show_chunks('fact_lecturas_trafico', older_than => INTERVAL '7 days')) s(c)" >&2
k8s_psql -c "ANALYZE trafico_calles; ANALYZE fact_flujo_aristas; ANALYZE fact_lecturas_trafico; ANALYZE dim_segmentos_viales"

# 7. Verificación por día: mismo resultado en Compose y en el cluster (cada uno con su propia red).
log "Verificando contra Compose"
VERIF="SET TIME ZONE 'UTC';
SELECT 'trafico_calles', t.timestamp_calculo::date, count(*), sum(t.nivel_congestion_inf::numeric),
       sum((c.u % 9973) * t.nivel_congestion_inf::numeric), count(DISTINCT t.metodo_inferencia)
FROM trafico_calles t JOIN dim_calles_osm c USING (osm_id)
WHERE t.timestamp_calculo < '$CORTE' GROUP BY 2 ORDER BY 2;
SELECT 'flujo', f.timestamp_lectura::date, count(*), sum(f.nivel_relativo::numeric),
       sum((c.u % 9973) * f.nivel_relativo::numeric), count(*) FILTER (WHERE f.cerrada)
FROM fact_flujo_aristas f JOIN dim_calles_osm c USING (osm_id)
WHERE f.timestamp_lectura < '$CORTE' GROUP BY 2 ORDER BY 2;
SELECT 'lecturas', timestamp_lectura::date, count(*), sum(velocidad_actual::numeric), sum(nivel_congestion::numeric),
       count(DISTINCT id_segmento)
FROM fact_lecturas_trafico WHERE timestamp_lectura < '$CORTE' GROUP BY 2 ORDER BY 2;
SELECT 'segmentos_de_compose_presentes', count(*) FROM dim_segmentos_viales;"
compose_psql -At -F' | ' <<< "$VERIF" > "$DIR/verif_compose.txt"
k8s_psql     -At -F' | ' <<< "$VERIF" > "$DIR/verif_k8s.txt"
# El cluster tiene además sus propios segmentos: esa última línea se compara aparte.
if diff <(grep -v '^segmentos' "$DIR/verif_compose.txt") <(grep -v '^segmentos' "$DIR/verif_k8s.txt") >&2; then
  log "OK: $(grep -vc '^segmentos' "$DIR/verif_k8s.txt") días×tabla idénticos (conteos y sumas exactas)"
else
  log "ERROR: el histórico del cluster no coincide con Compose (diff arriba)"; exit 1
fi
compose_psql -Atc "COPY (SELECT id_segmento FROM dim_segmentos_viales ORDER BY 1) TO STDOUT" > "$DIR/seg_compose.txt"
k8s_psql     -Atc "COPY (SELECT id_segmento FROM dim_segmentos_viales ORDER BY 1) TO STDOUT" > "$DIR/seg_k8s.txt"
faltan=$(comm -23 "$DIR/seg_compose.txt" "$DIR/seg_k8s.txt" | wc -l)
[[ "$faltan" == "0" ]] || { log "ERROR: faltan $faltan segmentos de Compose en el cluster"; exit 1; }
log "OK: los $(wc -l < "$DIR/seg_compose.txt") segmentos de Compose están en el cluster"
