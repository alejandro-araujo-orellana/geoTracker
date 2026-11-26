# Fase 2 · Etapa A — BD en CloudNativePG con PostgreSQL 17: plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** sustituir la BD del cluster local (StatefulSet, PG 15.2 / TimescaleDB 2.10.2) por un `Cluster` de
CloudNativePG con PostgreSQL 17 y versiones actuales de TimescaleDB, PostGIS y pgRouting, sin perder datos y
verificado igual que D43.

**Architecture:** imagen propia sobre la imagen PostGIS de CNPG con dos versiones de TimescaleDB (la "puente",
que existe también para PG 15, y la final). La BD vieja se actualiza a la versión puente en una **copia** (contenedor
Docker fuera del cluster), se vuelca, se restaura en el `Cluster` CNPG nuevo y ahí se actualizan las extensiones.
El `Cluster` nuevo convive con el `StatefulSet` hasta el corte; el corte repunta PgBouncer, el `Job` OSM y Grafana a
`geotracker-db-rw`.

**Tech Stack:** CloudNativePG (operador + `Cluster`), PostgreSQL 17, TimescaleDB (TSL), PostGIS, pgRouting,
Kustomize, k3d, Docker, bash, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-fase2-azure-aks-design.md` (§5.1). Etapas B–D tendrán su propio plan,
escrito al cerrar esta.

## Global Constraints

- Versiones exactas en todo: imágenes base por **digest**, paquetes `apt` con versión, operador y charts con versión
  (reglas del repo, D17).
- TimescaleDB debe ser la build **TSL** (community), no la Apache-only: el esquema usa compresión y continuous
  aggregates.
- Un `pg_dump` de TimescaleDB solo se restaura en **la misma versión** de la extensión; actualizar después con
  `ALTER EXTENSION timescaledb UPDATE` (spec §3).
- La BD viva nunca se modifica: la actualización intermedia ocurre en una copia. El `StatefulSet` viejo y su PVC se
  conservan hasta la tarea 8.
- **Nunca dos ETL**; durante el corte el `CronJob` está suspendido y sin `Job` activo.
- Esquema: no se crean ni editan migraciones en esta etapa (001–015 siguen igual); `migrate.py` debe quedar sin
  pendientes en la BD nueva.
- Instancias: **1** en el overlay local (spec §5.1). Recursos: `requests 512Mi/250m`, `limits 1Gi/1000m`; parámetros
  de memoria de D39 (`shared_buffers=256MB`, `effective_cache_size=768MB`, `work_mem=8MB`,
  `maintenance_work_mem=64MB`, `autovacuum_max_workers=3`, `max_parallel_workers_per_gather=1`,
  `max_parallel_workers=2`) en `spec.postgresql.parameters`.
- Nombres que no cambian: BD `vialidades_slp`, dueño `user`, rol `grafana_ro`, Secrets `geotracker-secret` y
  `grafana-secret`.
- Cada decisión, entrada en `docs/DECISIONES.md` (D45 en adelante) citada en el commit; mensajes de commit terminan
  con `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Comandos de cluster desde Windows: vía `wsl -d Ubuntu -- bash <script>` (el quoting por PowerShell falla con `$`).

## Review Focus

1. **Un ciclo del ETL que escribe en la BD vieja después del volcado final** → esos datos se pierden en silencio.
   Esperado: el corte se niega a seguir si el `CronJob` no está suspendido o hay un `Job` activo, y la última fila
   de `ejecuciones_etl` es la misma en ambas BD. (Tarea 6, paso 1.)
2. **Jobs de TimescaleDB ausentes tras la restauración** (políticas de compresión, retención y refresco del
   continuous aggregate) → la BD crece sin comprimir y el panel 1 deja de actualizarse sin error visible. Esperado:
   la lista `proc_name, hypertable_name, config` de `timescaledb_information.jobs` es idéntica. (Tarea 3, verificador.)
3. **`grafana_ro` sin contraseña o sin permisos tras la restauración lógica** (los roles no viajan en `pg_dump`) →
   dashboards vacíos y alertas en NoData. Esperado: `grafana_ro` hace `SELECT` en `trafico_calles` vía
   `geotracker-db-rw` y la datasource de Grafana responde OK. (Tarea 6, paso 5.)
4. **El equipo se apaga a diario**: tras `k3d cluster stop/start` el `Cluster` CNPG debe volver a `healthy` solo y
   el siguiente ciclo del ETL debe terminar `ok`. (Tarea 7, paso 3.)
5. **Conexiones sin TLS** (PgBouncer y Grafana usan `sslmode=disable`) rechazadas por el `pg_hba` de CNPG → la API
   responde 503 tras el corte. Esperado: ambos conectan; si CNPG lo impide, se ajusta `spec.postgresql.pg_hba`
   explícitamente, no se cambia el cliente. (Tarea 1 lo descubre; Tarea 5, paso 4 lo prueba.)

---

## Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `db/cnpg/Dockerfile` | Imagen final `geotracker-postgres:pg17-ts<FINAL>`: base PostGIS de CNPG + TimescaleDB puente y final + pgRouting |
| `db/puente/Dockerfile` | Imagen PG 15 con TimescaleDB puente + pgRouting 4.0.1 (solo para la copia intermedia) |
| `db/cnpg/servidor_pruebas.sh` | Arranca la imagen final como servidor PostgreSQL suelto en Docker (`localhost:5433`) para tests y pruebas de imagen |
| `db/cnpg/probar_imagen.sh` | Prueba de humo de la imagen: extensiones, actualización puente → final, funciones clave |
| `deploy/k8s/scripts/verificar_bd.sh` | Verificador reutilizable (sale de `migrar_historico_compose.sh`): compara dos BD por día/tabla, jobs, extensiones, migraciones, secuencias y ruta libre |
| `deploy/k8s/scripts/actualizar_bd_pg17.sh` | Orquesta la actualización: volcado → copia puente → volcado → restauración en CNPG → `ALTER EXTENSION` → verificación |
| `deploy/k8s/scripts/corte_bd_cnpg.sh` | Corte con guardas: suspende ETL, delta final, repunta, reanuda |
| `deploy/k8s/platform/cnpg/` | Instalación fija del operador CNPG (manifiesto oficial de la versión elegida) |
| `deploy/k8s/base/03b-database-cnpg.yaml` | `Cluster` CNPG reescrito |
| `deploy/k8s/overlays/local/kustomization.yaml` | Secret `geotracker-db-app` (basic-auth) derivado de `geotracker-secret` por `replacements` |
| `deploy/k8s/base/04-pgbouncer.yaml`, `09-osm-loader-job.yaml`, `platform/kube-prometheus-stack/values.yaml`, `platform/grafana/alerts/bd.yaml` | Repunte a `geotracker-db-rw` y nombre de PVC nuevo |

---

### Task 1: Descubrir versiones y el contrato de CNPG (spike con resultado registrado)

**Files:**
- Modify: `docs/DECISIONES.md` (entrada D45)

**Interfaces:**
- Produces: tabla en D45 con valores exactos que usan las tareas siguientes: `BASE_CNPG` (imagen + digest),
  `PG_FINAL` (17.x), `TS_PUENTE`, `TS_FINAL`, `POSTGIS_FINAL`, `PGR_FINAL`, `PGR_PUENTE` (para PG 15), `CNPG_VERSION`,
  nombres exactos de paquetes `apt`; respuesta a: ¿el `pg_hba` por defecto de CNPG acepta conexiones sin TLS?,
  ¿CNPG acepta el dueño `user` (palabra reservada)?, ¿`postgres` superusuario disponible con
  `enableSuperuserAccess: true`?

- [ ] **Step 1: Listar versiones disponibles**

Run (en contenedores desechables, nada se instala en el host):
`docker run --rm <BASE_CNPG candidata> bash -c "apt-get update -qq && apt-cache madison timescaledb-2-postgresql-17 postgresql-17-pgrouting"`
y lo mismo para `timescaledb-2-postgresql-15` en la base PG 15 de la imagen puente.
Expected: `TS_PUENTE` = la versión **más alta presente para PG 15 y PG 17 a la vez**; `TS_FINAL` = la más alta para PG 17.

- [ ] **Step 2: Probar el contrato de CNPG en k3d con un `Cluster` desechable**

Instalar el operador `CNPG_VERSION` (manifiesto oficial), crear `Cluster` `cnpg-prueba` (1 instancia, imagen
`BASE_CNPG`, `bootstrap.initdb` con dueño `user`), y comprobar: el pod llega a `Ready`; `psql "sslmode=disable"`
desde un pod del namespace a `cnpg-prueba-rw` conecta; `postgres` entra con `enableSuperuserAccess: true`.
Borrar `cnpg-prueba` y su PVC al terminar.
Expected: tres respuestas sí/no anotadas.

- [ ] **Step 3: Escribir D45** (versiones, digest, respuestas del paso 2, y por qué `TS_PUENTE`)

- [ ] **Step 4: Commit**

```bash
git add docs/DECISIONES.md
git commit -m "docs(bd): versiones de PG 17 / TimescaleDB / PostGIS / pgRouting y contrato de CNPG (D45)"
```

---

### Task 2: Imágenes final y puente, con prueba de humo

**Files:**
- Create: `db/cnpg/Dockerfile`, `db/puente/Dockerfile`, `db/cnpg/servidor_pruebas.sh`, `db/cnpg/probar_imagen.sh`

**Interfaces:**
- Consumes: valores de D45.
- Produces: imágenes `geotracker-postgres:pg17-ts<TS_FINAL>` y `geotracker-puente:pg15-ts<TS_PUENTE>`;
  `servidor_pruebas.sh [arrancar|parar] [imagen]` → PostgreSQL en `localhost:5433`, usuario `user`, BD
  `vialidades_slp`, contraseña de `$DB_PASSWORD`, `shared_preload_libraries=timescaledb`.

- [ ] **Step 1: Escribir `probar_imagen.sh` (la prueba) antes que el Dockerfile**

Contra el servidor de `servidor_pruebas.sh`, en este orden, abortando al primer fallo:
`CREATE EXTENSION timescaledb VERSION '<TS_PUENTE>'` → `CREATE EXTENSION postgis` → `CREATE EXTENSION pgrouting`
→ `ALTER EXTENSION timescaledb UPDATE` → aserciones:
`SELECT extversion FROM pg_extension WHERE extname='timescaledb'` = `TS_FINAL`;
`SELECT postgis_lib_version()` = `POSTGIS_FINAL`; `SELECT pgr_version()` empieza por `PGR_FINAL`;
una hypertable con `compress` y `compress_chunk` funciona (prueba la build TSL);
`SELECT version()` contiene `PostgreSQL 17`.

- [ ] **Step 2: Ejecutarla contra una imagen inexistente para verla fallar**

Run: `db/cnpg/probar_imagen.sh geotracker-postgres:pg17-ts<TS_FINAL>`
Expected: FAIL (`Unable to find image`).

- [ ] **Step 3: Escribir `db/cnpg/Dockerfile`** — `FROM <BASE_CNPG>@sha256:…`, instalar con versión exacta los paquetes
de `TS_PUENTE` y `TS_FINAL` (las dos `.so` conviven: el cargador elige por `extversion`) y `PGR_FINAL`; volver al
usuario `26` (postgres de CNPG). Encabezado con el porqué, como `db/Dockerfile` (D17).

- [ ] **Step 4: Escribir `db/puente/Dockerfile`** — base PG 15 de `timescale/timescaledb-ha` por digest con
`TS_PUENTE` disponible + `postgresql-15-pgrouting=<PGR_PUENTE>`. Encabezado: "solo para la actualización (D46);
no se despliega".

- [ ] **Step 5: Construir y pasar la prueba**

Run: `docker build -t geotracker-postgres:pg17-ts<TS_FINAL> db/cnpg && db/cnpg/probar_imagen.sh geotracker-postgres:pg17-ts<TS_FINAL>`
Expected: todas las aserciones OK, exit 0. Construir también la puente y comprobar a mano que
`CREATE EXTENSION timescaledb VERSION '2.10.2'` + `ALTER EXTENSION timescaledb UPDATE TO '<TS_PUENTE>'` funciona.

- [ ] **Step 6: Commit**

```bash
git add db/cnpg db/puente
git commit -m "feat(bd): imagen PG 17 para CloudNativePG e imagen puente PG 15 (D45)"
```

---

### Task 3: Verificador reutilizable `verificar_bd.sh`

**Files:**
- Create: `deploy/k8s/scripts/verificar_bd.sh`
- Modify: `deploy/k8s/scripts/migrar_historico_compose.sh` (paso 7 pasa a llamar al verificador)

**Interfaces:**
- Produces: `verificar_bd.sh` con variables de entorno `ORIGEN_PSQL` y `DESTINO_PSQL` (prefijo de comando que lee
  SQL por stdin, p. ej. `kubectl exec -i -n geotracker geotracker-db-0 -- sh -c 'psql …'`), `HASTA` (timestamptz,
  límite superior exclusivo) y `MODO` (`historico` = solo datos por día, como D43; `completo` = además lo de abajo).
  Exit 0 si todo coincide; 1 con el `diff` en stderr.
- Comparaciones en `completo`: las de D43 (por día y tabla: conteos, sumas `numeric`, suma ponderada por `u`);
  `timescaledb_information.jobs` (`proc_name, hypertable_name, schedule_interval, config`, ordenado);
  `schema_migrations`; `last_value` de cada secuencia; conteo de `dim_*`, `consumo_api`, `ejecuciones_etl`;
  conteo de `resumen_trafico_ciclo`; ruta a flujo libre entre los nodos de la ruta de prueba de D43 (82 nodos,
  426.979 s, mismo `md5`). **No** compara versiones de extensiones (difieren a propósito); las imprime.

- [ ] **Step 1: Caso que debe fallar** — correr `MODO=completo` con `ORIGEN_PSQL` = BD del cluster y `DESTINO_PSQL` =
servidor de `servidor_pruebas.sh` vacío con el esquema aplicado (`migrate.py`).
Expected: exit 1, `diff` con los conteos.

- [ ] **Step 2: Implementar** extrayendo las consultas del paso 7 de `migrar_historico_compose.sh` y añadiendo las de
`completo`.

- [ ] **Step 3: Caso que debe pasar** — `ORIGEN_PSQL` y `DESTINO_PSQL` apuntando ambos a la BD del cluster.
Expected: exit 0.

- [ ] **Step 4: Regresión de D43** — `migrar_historico_compose.sh` (que ahora llama a `verificar_bd.sh
MODO=historico`) contra Compose levantado con `--profile compose`. Expected: `27 días×tabla idénticos`. Volver a
apagar Compose.

- [ ] **Step 5: Commit**

```bash
git add deploy/k8s/scripts/verificar_bd.sh deploy/k8s/scripts/migrar_historico_compose.sh
git commit -m "refactor(k8s): verificador de BD reutilizable para la actualización y el ensayo de restauración"
```

---

### Task 4: Operador CNPG y `Cluster` nuevo junto al viejo

**Files:**
- Create: `deploy/k8s/platform/cnpg/` (manifiesto del operador `CNPG_VERSION` + `README` de una línea con el origen)
- Modify: `deploy/k8s/base/03b-database-cnpg.yaml`, `deploy/k8s/base/kustomization.yaml`,
  `deploy/k8s/overlays/local/kustomization.yaml`

**Interfaces:**
- Produces: `Cluster` `geotracker-db` (Services `geotracker-db-rw`, `-ro`, `-r`; pod `geotracker-db-1`; PVC
  `geotracker-db-1`), BD `vialidades_slp` vacía con dueño `user`; Secret `geotracker-db-app`
  (`kubernetes.io/basic-auth`, `username: user`, `password` copiado de `geotracker-secret/DB_PASSWORD`);
  rol administrado `grafana_ro` con contraseña desde `grafana-secret/GRAFANA_DB_PASSWORD`; superusuario `postgres`
  accesible (`enableSuperuserAccess: true`).

- [ ] **Step 1: Comprobar que hoy no existe** — `kubectl get cluster -n geotracker` → "No resources found" / CRD ausente.

- [ ] **Step 2: Reescribir `03b`** con: `imageName` final, `instances: 1` en base local (la nube parcheará a 2 en la
etapa C), `shared_preload_libraries: [timescaledb]`, parámetros de D39, recursos de Global Constraints,
`storage.size: 10Gi`, `bootstrap.initdb` (`database: vialidades_slp`, `owner: user`, `secret: geotracker-db-app`),
`managed.roles` con `grafana_ro`, `enableSuperuserAccess: true`, `pg_hba` solo si D45 lo exige. Traducir los
comentarios en inglés al estilo del resto (cabecera con el porqué).

- [ ] **Step 3: `kustomization`** — base incluye **03 y 03b** durante la transición (comentario: "03 se retira en la
tarea 8"); overlay local añade el Secret `geotracker-db-app` y un `replacements` desde `geotracker-secret` (sin
duplicar la contraseña en otro archivo, D27).

- [ ] **Step 4: Aplicar** — operador (`kubectl apply --server-side -f deploy/k8s/platform/cnpg/…`), luego
`kubectl apply -k deploy/k8s/overlays/local`.
Expected: `kubectl get cluster geotracker-db -n geotracker` → `Cluster in healthy state`; `geotracker-db-0` (viejo)
sigue `Running`; API y ETL sin cambios (siguen en la BD vieja vía PgBouncer).

- [ ] **Step 5: Probar conexiones del Review Focus 5** desde un pod efímero: `psql "host=geotracker-db-rw
sslmode=disable user=user"` y `user=grafana_ro`. Expected: ambos conectan.

- [ ] **Step 6: Commit** (con D46 si el paso 2 tomó decisiones: Secret por `replacements`, roles administrados)

```bash
git add deploy/k8s docs/DECISIONES.md
git commit -m "feat(k8s): operador CloudNativePG y Cluster geotracker-db junto al StatefulSet (D46)"
```

---

### Task 5: Actualización ensayada sobre copia

**Files:**
- Create: `deploy/k8s/scripts/actualizar_bd_pg17.sh`

**Interfaces:**
- Consumes: imágenes de la tarea 2, `verificar_bd.sh` (tarea 3), `Cluster` de la tarea 4.
- Produces: `actualizar_bd_pg17.sh [--ensayo|--final]`. `--ensayo`: todo menos el corte; deja el `Cluster` con los
  datos y corre `verificar_bd.sh MODO=completo HASTA=<instante del volcado>`. `--final`: lo usa la tarea 6.
  Re-ejecutable: si el `Cluster` ya tiene datos, lo vacía (DROP/CREATE de `vialidades_slp` como `postgres`) antes de
  restaurar, **nunca** toca `geotracker-db-0`.

Pasos que el script ejecuta (orden fijo, abortar al primer error):
1. `pg_dump -Fc` de `geotracker-db-0` → `~/geotracker-respaldos/antes-pg17-<ts>.dump` (+ `pg_restore -l` legible).
2. Contenedor de `geotracker-puente`: `initdb`, `CREATE ROLE "user"`/`grafana_ro`, `timescaledb_pre_restore()`,
   `pg_restore`, `timescaledb_post_restore()`, `ALTER EXTENSION timescaledb UPDATE TO '<TS_PUENTE>'`.
3. `pg_dump -Fc` de esa copia.
4. En el `Cluster` (como `postgres`): `CREATE EXTENSION timescaledb VERSION '<TS_PUENTE>'`,
   `timescaledb_pre_restore()`, `pg_restore --no-owner --role=user`, `timescaledb_post_restore()`.
5. `ALTER EXTENSION timescaledb UPDATE` (→ `TS_FINAL`), `ALTER EXTENSION postgis UPDATE`,
   `ALTER EXTENSION pgrouting UPDATE`; `CALL refresh_continuous_aggregate('resumen_trafico_ciclo', NULL, NULL)`;
   `ANALYZE`.
6. `python src/migrate.py` contra el `Cluster` (port-forward a `geotracker-db-rw`) → "sin migraciones pendientes".
7. `verificar_bd.sh MODO=completo`.

- [ ] **Step 1: Correr `--ensayo` sin el script → falla** (archivo inexistente). Expected: FAIL.

- [ ] **Step 2: Implementar el script** (pasos 1–7 de arriba).

- [ ] **Step 3: Correr `--ensayo`**. Expected: verificador exit 0; tiempos de cada paso impresos; `geotracker-db-0`
intacto (`schema_migrations` y conteos iguales a antes).

- [ ] **Step 4: Tests de integración contra la versión nueva** — `db/cnpg/servidor_pruebas.sh arrancar` y
`DB_HOST=localhost DB_PORT=5433 PYTHONPATH=src pytest tests` (en la imagen del ETL como indica `CLAUDE.md`, pero
con `--network host` en lugar de `--network geotracker_red_trafico` para alcanzar `localhost:5433`), más
`PYTHONPATH=src:services/api pytest services/api/tests`. Expected: todos pasan; los `*_db.py` **no** se saltan.

- [ ] **Step 5: Correr `--ensayo` una segunda vez** (re-ejecución). Expected: igual que el paso 3.

- [ ] **Step 6: Commit**

```bash
git add deploy/k8s/scripts/actualizar_bd_pg17.sh
git commit -m "feat(bd): actualización a PG 17 vía TimescaleDB puente, ensayada y verificada (D46)"
```

---

### Task 6: Corte a la BD nueva

**Files:**
- Create: `deploy/k8s/scripts/corte_bd_cnpg.sh`
- Modify: `deploy/k8s/base/04-pgbouncer.yaml` (host `geotracker-db-rw`), `deploy/k8s/base/09-osm-loader-job.yaml`
  (2× `DB_HOST`), `deploy/k8s/platform/kube-prometheus-stack/values.yaml` (`url:
  geotracker-db-rw.geotracker.svc.cluster.local:5432`, `postgresVersion: 1700`),
  `deploy/k8s/platform/grafana/alerts/bd.yaml` (PVC `geotracker-db-1`)

**Interfaces:**
- Consumes: `actualizar_bd_pg17.sh --final`, `verificar_bd.sh`.

- [ ] **Step 1: Guardas del corte (Review Focus 1)** — el script empieza suspendiendo el `CronJob` y **aborta** si
`kubectl get jobs -n geotracker -l <etiqueta del CronJob>` muestra uno activo; espera a que termine (máx. 5 min).
Prueba: lanzar el script mientras corre un ciclo manual (`kubectl create job --from=cronjob/geotracker-etl
prueba-corte`). Expected: espera o aborta con mensaje; no vuelca a medias.

- [ ] **Step 2: Implementar** — suspender → `actualizar_bd_pg17.sh --final` (volcado sin ciclos en curso) →
verificar que la última fila de `ejecuciones_etl` coincide en ambas → `kubectl apply -k` con los repuntes de esta
tarea + `helm upgrade` de kps con los values → `rollout restart` de `pgbouncer` y `geotracker-api` → reanudar el
`CronJob`.

- [ ] **Step 3: Ejecutar el corte**. Expected: script exit 0.

- [ ] **Step 4: Ruta real** — `kubectl port-forward svc/geotracker-api 8000:80` + la consulta de `CLAUDE.md`.
Expected: 200 y `trafico.vigente: true` tras el siguiente ciclo.

- [ ] **Step 5: Grafana (Review Focus 3)** — `GET /api/datasources/uid/timescaledb/health` en el Grafana del cluster
→ `OK`; el panel 1 muestra datos de la última hora; reglas en estado Normal.

- [ ] **Step 6: Primer ciclo** — esperar el siguiente `*/15`. Expected: fila nueva en `ejecuciones_etl` de la BD
nueva con `estado = 'ok'`; ninguna fila nueva en la vieja.

- [ ] **Step 7: Commit** (D47: el corte, tiempos, ventana sin ETL)

```bash
git add deploy/k8s docs/DECISIONES.md
git commit -m "feat(k8s): corte de la BD al Cluster CloudNativePG (PG 17) (D47)"
```

---

### Task 7: Resistencia al apagado diario y a la caída del pod

**Files:**
- Modify: `docs/KUBERNETES.md` ("Observabilidad: operación" → nota de operación de CNPG)

- [ ] **Step 1: Borrar el pod** — `kubectl delete pod geotracker-db-1 -n geotracker`. Expected: CNPG lo recrea, el
`Cluster` vuelve a `healthy`, `/health/ready` de la API vuelve a 200 en < 2 min.

- [ ] **Step 2: Registrar** el tiempo medido.

- [ ] **Step 3: Apagado (Review Focus 4)** — `k3d cluster stop geotracker && k3d cluster start geotracker`.
Expected: `Cluster` `healthy` sin intervención; el siguiente ciclo `ok`; la alerta "ETL sin correr" se resuelve sola.

- [ ] **Step 4: Documentar** comandos de operación (`kubectl get cluster`, plugin `kubectl cnpg status` si se
instala con versión fija, consola SQL nueva) y commit.

```bash
git add docs/KUBERNETES.md
git commit -m "docs(k8s): operación de la BD en CloudNativePG (reinicio, consola, estado)"
```

---

### Task 8: Retirar el `StatefulSet` y cerrar la etapa

**Files:**
- Delete: `deploy/k8s/base/03-database-statefulset.yaml`
- Modify: `deploy/k8s/base/kustomization.yaml`, `CLAUDE.md`, `docs/DATABASE.md`, `docs/KUBERNETES.md`
  (diagrama de despliegue y "Base de datos"), `docs/ARCHITECTURE.md`, `docs/DECISIONES.md` (D48),
  `db/Dockerfile` (cabecera: "imagen retirada; solo para restaurar respaldos anteriores a D47")

- [ ] **Step 1: Condición previa** — al menos 2 días con ciclos `ok` en la BD nueva y un respaldo `antes-pg17-*.dump`
legible. Si no se cumple, no se avanza.

- [ ] **Step 2: Retirar** — quitar 03 de `base`; `kubectl delete statefulset geotracker-db -n geotracker` y
`kubectl delete svc geotracker-db -n geotracker`; **conservar** el PVC `datos-postgres-geotracker-db-0` (se borra a
mano tras la etapa B, cuando haya respaldos fuera de casa).

- [ ] **Step 3: Docs** — `CLAUDE.md` (aviso de imagen fijada → la nueva; consola SQL vía `geotracker-db-rw`/pod
`geotracker-db-1`; tests de integración con `db/cnpg/servidor_pruebas.sh` en vez de la BD de Compose);
`DATABASE.md` (motor y extensiones); diagramas Mermaid de `KUBERNETES.md` actualizados y re-validados con
mermaid-cli (12/12 OK).

- [ ] **Step 4: Verificación final** — `PYTHONPATH=src pytest tests` contra `servidor_pruebas.sh`;
`kubectl get pods -n geotracker` sin `geotracker-db-0`; API 200; ciclo `ok`.

- [ ] **Step 5: Commit**

```bash
git add -A deploy/k8s db CLAUDE.md docs
git commit -m "chore(k8s): StatefulSet de la BD retirado; etapa A de la fase 2 cerrada (D48)"
```
