# Phase 2 · Stage A — Database on CloudNativePG with PostgreSQL 17: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** replace the local cluster's database (StatefulSet, PG 15.2 / TimescaleDB 2.10.2) with a CloudNativePG
`Cluster` running PostgreSQL 17 and current versions of TimescaleDB, PostGIS and pgRouting, without losing data and
verified the same way as D43.

**Architecture:** our own image on top of CNPG's PostGIS image with two TimescaleDB versions (the "bridge" one,
which also exists for PG 15, and the final one). The old database is upgraded to the bridge version on a **copy** (a Docker
container outside the cluster), dumped, restored into the new CNPG `Cluster`, and the extensions are upgraded there.
The new `Cluster` coexists with the `StatefulSet` until the cut-over; the cut-over repoints PgBouncer, the OSM `Job` and Grafana to
`geotracker-db-rw`.

**Tech Stack:** CloudNativePG (operator + `Cluster`), PostgreSQL 17, TimescaleDB (TSL), PostGIS, pgRouting,
Kustomize, k3d, Docker, bash, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-fase2-azure-aks-design.md` (§5.1). Stages B–D will have their own plan,
written when this one closes.

## Global Constraints

- Exact versions everywhere: base images by **digest**, `apt` packages with version, operator and charts with version
  (repo rules, D17).
- TimescaleDB must be the **TSL** (community) build, not the Apache-only one: the schema uses compression and continuous
  aggregates.
- A TimescaleDB `pg_dump` can only be restored on **the same version** of the extension; upgrade afterwards with
  `ALTER EXTENSION timescaledb UPDATE` (spec §3).
- The live database is never modified: the intermediate upgrade happens on a copy. The old `StatefulSet` and its PVC are
  kept until task 8.
- **Never two ETLs**; during the cut-over the `CronJob` is suspended and has no active `Job`.
- Schema: no migrations are created or edited in this stage (001–015 stay the same); `migrate.py` must have nothing
  pending on the new database.
- Instances: **1** in the local overlay (spec §5.1). Resources: `requests 512Mi/250m`, `limits 1Gi/1000m`; D39's memory
  parameters (`shared_buffers=256MB`, `effective_cache_size=768MB`, `work_mem=8MB`,
  `maintenance_work_mem=64MB`, `autovacuum_max_workers=3`, `max_parallel_workers_per_gather=1`,
  `max_parallel_workers=2`) in `spec.postgresql.parameters`.
- Names that do not change: database `vialidades_slp`, owner `user`, role `grafana_ro`, Secrets `geotracker-secret` and
  `grafana-secret`.
- Every decision gets an entry in `docs/DECISIONES.md` (D45 onwards) cited in the commit; commit messages end
  with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Cluster commands from Windows: via `wsl -d Ubuntu -- bash <script>` (PowerShell quoting breaks with `$`).

## Review Focus

1. **An ETL cycle that writes to the old database after the final dump** → that data is silently lost.
   Expected: the cut-over refuses to continue if the `CronJob` is not suspended or there is an active `Job`, and the last row
   of `ejecuciones_etl` is the same in both databases. (Task 6, step 1.)
2. **TimescaleDB jobs missing after the restore** (compression, retention and continuous aggregate refresh
   policies) → the database grows uncompressed and panel 1 stops updating with no visible error. Expected:
   the `proc_name, hypertable_name, config` list from `timescaledb_information.jobs` is identical. (Task 3, verifier.)
3. **`grafana_ro` without a password or permissions after the logical restore** (roles do not travel in `pg_dump`) →
   empty dashboards and alerts in NoData. Expected: `grafana_ro` can `SELECT` on `trafico_calles` via
   `geotracker-db-rw` and Grafana's datasource answers OK. (Task 6, step 5.)
4. **The computer is switched off daily**: after `k3d cluster stop/start` the CNPG `Cluster` must return to `healthy` by itself and
   the next ETL cycle must end `ok`. (Task 7, step 3.)
5. **Non-TLS connections** (PgBouncer and Grafana use `sslmode=disable`) rejected by CNPG's `pg_hba` → the API
   returns 503 after the cut-over. Expected: both connect; if CNPG prevents it, `spec.postgresql.pg_hba` is adjusted
   explicitly, the client is not changed. (Task 1 discovers it; Task 5, step 4 tests it.)

---

## File structure

| File | Responsibility |
|---|---|
| `db/cnpg/Dockerfile` | Final image `geotracker-postgres:pg17-ts<FINAL>`: CNPG PostGIS base + bridge and final TimescaleDB + pgRouting |
| `db/puente/Dockerfile` | PG 15 image with bridge TimescaleDB + pgRouting 4.0.1 (only for the intermediate copy) |
| `db/cnpg/servidor_pruebas.sh` | Starts the final image as a standalone PostgreSQL server in Docker (`localhost:5433`) for tests and image checks |
| `db/cnpg/probar_imagen.sh` | Image smoke test: extensions, bridge → final upgrade, key functions |
| `deploy/k8s/scripts/verificar_bd.sh` | Reusable verifier (extracted from `migrar_historico_compose.sh`): compares two databases per day/table, jobs, extensions, migrations, sequences and free-flow route |
| `deploy/k8s/scripts/actualizar_bd_pg17.sh` | Orchestrates the upgrade: dump → bridge copy → dump → restore into CNPG → `ALTER EXTENSION` → verification |
| `deploy/k8s/scripts/corte_bd_cnpg.sh` | Guarded cut-over: suspends the ETL, final delta, repoints, resumes |
| `deploy/k8s/platform/cnpg/` | Pinned installation of the CNPG operator (official manifest of the chosen version) |
| `deploy/k8s/base/03b-database-cnpg.yaml` | Rewritten CNPG `Cluster` |
| `deploy/k8s/overlays/local/kustomization.yaml` | `geotracker-db-app` Secret (basic-auth) derived from `geotracker-secret` via `replacements` |
| `deploy/k8s/base/04-pgbouncer.yaml`, `09-osm-loader-job.yaml`, `platform/kube-prometheus-stack/values.yaml`, `platform/grafana/alerts/bd.yaml` | Repoint to `geotracker-db-rw` and new PVC name |

---

### Task 1: Discover versions and the CNPG contract (spike with a recorded result)

**Files:**
- Modify: `docs/DECISIONES.md` (entry D45)

**Interfaces:**
- Produces: a table in D45 with exact values used by the following tasks: `BASE_CNPG` (image + digest),
  `PG_FINAL` (17.x), `TS_PUENTE`, `TS_FINAL`, `POSTGIS_FINAL`, `PGR_FINAL`, `PGR_PUENTE` (for PG 15), `CNPG_VERSION`,
  exact `apt` package names; answers to: does CNPG's default `pg_hba` accept non-TLS connections?,
  does CNPG accept the owner `user` (a reserved word)?, is the `postgres` superuser available with
  `enableSuperuserAccess: true`?

- [ ] **Step 1: List available versions**

Run (in throwaway containers, nothing is installed on the host):
`docker run --rm <candidate BASE_CNPG> bash -c "apt-get update -qq && apt-cache madison timescaledb-2-postgresql-17 postgresql-17-pgrouting"`
and the same for `timescaledb-2-postgresql-15` on the bridge image's PG 15 base.
Expected: `TS_PUENTE` = the **highest version present for both PG 15 and PG 17**; `TS_FINAL` = the highest for PG 17.

- [ ] **Step 2: Test the CNPG contract on k3d with a throwaway `Cluster`**

Install the `CNPG_VERSION` operator (official manifest), create `Cluster` `cnpg-prueba` (1 instance, image
`BASE_CNPG`, `bootstrap.initdb` with owner `user`), and check: the pod reaches `Ready`; `psql "sslmode=disable"`
from a pod in the namespace to `cnpg-prueba-rw` connects; `postgres` logs in with `enableSuperuserAccess: true`.
Delete `cnpg-prueba` and its PVC when done.
Expected: three yes/no answers recorded.

- [ ] **Step 3: Write D45** (versions, digest, answers from step 2, and why `TS_PUENTE`)

- [ ] **Step 4: Commit**

```bash
git add docs/DECISIONES.md
git commit -m "docs(db): PG 17 / TimescaleDB / PostGIS / pgRouting versions and CNPG contract (D45)"
```

---

### Task 2: Final and bridge images, with a smoke test

**Files:**
- Create: `db/cnpg/Dockerfile`, `db/puente/Dockerfile`, `db/cnpg/servidor_pruebas.sh`, `db/cnpg/probar_imagen.sh`

**Interfaces:**
- Consumes: values from D45.
- Produces: images `geotracker-postgres:pg17-ts<TS_FINAL>` and `geotracker-puente:pg15-ts<TS_PUENTE>`;
  `servidor_pruebas.sh [arrancar|parar] [image]` → PostgreSQL on `localhost:5433`, user `user`, database
  `vialidades_slp`, password from `$DB_PASSWORD`, `shared_preload_libraries=timescaledb`.

- [ ] **Step 1: Write `probar_imagen.sh` (the test) before the Dockerfile**

Against the `servidor_pruebas.sh` server, in this order, aborting on the first failure:
`CREATE EXTENSION timescaledb VERSION '<TS_PUENTE>'` → `CREATE EXTENSION postgis` → `CREATE EXTENSION pgrouting`
→ `ALTER EXTENSION timescaledb UPDATE` → assertions:
`SELECT extversion FROM pg_extension WHERE extname='timescaledb'` = `TS_FINAL`;
`SELECT postgis_lib_version()` = `POSTGIS_FINAL`; `SELECT pgr_version()` starts with `PGR_FINAL`;
a hypertable with `compress` and `compress_chunk` works (tests the TSL build);
`SELECT version()` contains `PostgreSQL 17`.

- [ ] **Step 2: Run it against a nonexistent image to see it fail**

Run: `db/cnpg/probar_imagen.sh geotracker-postgres:pg17-ts<TS_FINAL>`
Expected: FAIL (`Unable to find image`).

- [ ] **Step 3: Write `db/cnpg/Dockerfile`** — `FROM <BASE_CNPG>@sha256:…`, install the `TS_PUENTE` and `TS_FINAL`
packages at exact versions (both `.so` files coexist: the loader picks by `extversion`) and `PGR_FINAL`; switch back to
user `26` (CNPG's postgres). Header with the rationale, like `db/Dockerfile` (D17).

- [ ] **Step 4: Write `db/puente/Dockerfile`** — PG 15 base of `timescale/timescaledb-ha` by digest with
`TS_PUENTE` available + `postgresql-15-pgrouting=<PGR_PUENTE>`. Header: "only for the upgrade (D46);
not deployed".

- [ ] **Step 5: Build and pass the test**

Run: `docker build -t geotracker-postgres:pg17-ts<TS_FINAL> db/cnpg && db/cnpg/probar_imagen.sh geotracker-postgres:pg17-ts<TS_FINAL>`
Expected: all assertions OK, exit 0. Also build the bridge image and check by hand that
`CREATE EXTENSION timescaledb VERSION '2.10.2'` + `ALTER EXTENSION timescaledb UPDATE TO '<TS_PUENTE>'` works.

- [ ] **Step 6: Commit**

```bash
git add db/cnpg db/puente
git commit -m "feat(db): PG 17 image for CloudNativePG and PG 15 bridge image (D45)"
```

---

### Task 3: Reusable verifier `verificar_bd.sh`

**Files:**
- Create: `deploy/k8s/scripts/verificar_bd.sh`
- Modify: `deploy/k8s/scripts/migrar_historico_compose.sh` (step 7 now calls the verifier)

**Interfaces:**
- Produces: `verificar_bd.sh` with environment variables `ORIGEN_PSQL` and `DESTINO_PSQL` (a command prefix that reads
  SQL from stdin, e.g. `kubectl exec -i -n geotracker geotracker-db-0 -- sh -c 'psql …'`), `HASTA` (timestamptz,
  exclusive upper bound) and `MODO` (`historico` = per-day data only, as in D43; `completo` = plus the items below).
  Exit 0 if everything matches; 1 with the `diff` on stderr.
- Comparisons in `completo`: D43's (per day and table: counts, `numeric` sums, sum weighted by `u`);
  `timescaledb_information.jobs` (`proc_name, hypertable_name, schedule_interval, config`, sorted);
  `schema_migrations`; `last_value` of each sequence; count of `dim_*`, `consumo_api`, `ejecuciones_etl`;
  count of `resumen_trafico_ciclo`; free-flow route between the nodes of D43's test route (82 nodes,
  426.979 s, same `md5`). It does **not** compare extension versions (they differ on purpose); it prints them.

- [ ] **Step 1: Case that must fail** — run `MODO=completo` with `ORIGEN_PSQL` = the cluster's database and `DESTINO_PSQL` =
an empty `servidor_pruebas.sh` server with the schema applied (`migrate.py`).
Expected: exit 1, `diff` with the counts.

- [ ] **Step 2: Implement** by extracting the queries from step 7 of `migrar_historico_compose.sh` and adding the
`completo` ones.

- [ ] **Step 3: Case that must pass** — `ORIGEN_PSQL` and `DESTINO_PSQL` both pointing to the cluster's database.
Expected: exit 0.

- [ ] **Step 4: D43 regression** — `migrar_historico_compose.sh` (which now calls `verificar_bd.sh
MODO=historico`) against Compose brought up with `--profile compose`. Expected: `27 días×tabla idénticos`. Shut
Compose down again.

- [ ] **Step 5: Commit**

```bash
git add deploy/k8s/scripts/verificar_bd.sh deploy/k8s/scripts/migrar_historico_compose.sh
git commit -m "refactor(k8s): reusable database verifier for the upgrade and the restore drill"
```

---

### Task 4: CNPG operator and new `Cluster` next to the old one

**Files:**
- Create: `deploy/k8s/platform/cnpg/` (`CNPG_VERSION` operator manifest + a one-line `README` with its origin)
- Modify: `deploy/k8s/base/03b-database-cnpg.yaml`, `deploy/k8s/base/kustomization.yaml`,
  `deploy/k8s/overlays/local/kustomization.yaml`

**Interfaces:**
- Produces: `Cluster` `geotracker-db` (Services `geotracker-db-rw`, `-ro`, `-r`; pod `geotracker-db-1`; PVC
  `geotracker-db-1`), empty database `vialidades_slp` owned by `user`; Secret `geotracker-db-app`
  (`kubernetes.io/basic-auth`, `username: user`, `password` copied from `geotracker-secret/DB_PASSWORD`);
  managed role `grafana_ro` with its password from `grafana-secret/GRAFANA_DB_PASSWORD`; `postgres` superuser
  accessible (`enableSuperuserAccess: true`).

- [ ] **Step 1: Check that it does not exist today** — `kubectl get cluster -n geotracker` → "No resources found" / CRD missing.

- [ ] **Step 2: Rewrite `03b`** with: final `imageName`, `instances: 1` in the local base (the cloud will patch it to 2 in
stage C), `shared_preload_libraries: [timescaledb]`, D39 parameters, Global Constraints resources,
`storage.size: 10Gi`, `bootstrap.initdb` (`database: vialidades_slp`, `owner: user`, `secret: geotracker-db-app`),
`managed.roles` with `grafana_ro`, `enableSuperuserAccess: true`, `pg_hba` only if D45 requires it. Keep the
comments in the same style as the rest (header with the rationale).

- [ ] **Step 3: `kustomization`** — base includes **03 and 03b** during the transition (comment: "03 is retired in
task 8"); the local overlay adds the `geotracker-db-app` Secret and a `replacements` from `geotracker-secret` (without
duplicating the password in another file, D27).

- [ ] **Step 4: Apply** — operator (`kubectl apply --server-side -f deploy/k8s/platform/cnpg/…`), then
`kubectl apply -k deploy/k8s/overlays/local`.
Expected: `kubectl get cluster geotracker-db -n geotracker` → `Cluster in healthy state`; `geotracker-db-0` (old)
still `Running`; API and ETL unchanged (still on the old database via PgBouncer).

- [ ] **Step 5: Test the Review Focus 5 connections** from an ephemeral pod: `psql "host=geotracker-db-rw
sslmode=disable user=user"` and `user=grafana_ro`. Expected: both connect.

- [ ] **Step 6: Commit** (with D46 if step 2 took decisions: Secret via `replacements`, managed roles)

```bash
git add deploy/k8s docs/DECISIONES.md
git commit -m "feat(k8s): CloudNativePG operator and geotracker-db Cluster next to the StatefulSet (D46)"
```

---

### Task 5: Rehearsed upgrade on a copy

**Files:**
- Create: `deploy/k8s/scripts/actualizar_bd_pg17.sh`

**Interfaces:**
- Consumes: images from task 2, `verificar_bd.sh` (task 3), `Cluster` from task 4.
- Produces: `actualizar_bd_pg17.sh [--ensayo|--final]`. `--ensayo`: everything except the cut-over; leaves the `Cluster` with the
  data and runs `verificar_bd.sh MODO=completo HASTA=<dump instant>`. `--final`: used by task 6.
  Re-runnable: if the `Cluster` already has data, it empties it (DROP/CREATE of `vialidades_slp` as `postgres`) before
  restoring, and **never** touches `geotracker-db-0`.

Steps the script runs (fixed order, abort on the first error):
1. `pg_dump -Fc` of `geotracker-db-0` → `~/geotracker-respaldos/antes-pg17-<ts>.dump` (+ readable `pg_restore -l`).
2. `geotracker-puente` container: `initdb`, `CREATE ROLE "user"`/`grafana_ro`, `timescaledb_pre_restore()`,
   `pg_restore`, `timescaledb_post_restore()`, `ALTER EXTENSION timescaledb UPDATE TO '<TS_PUENTE>'`.
3. `pg_dump -Fc` of that copy.
4. In the `Cluster` (as `postgres`): `CREATE EXTENSION timescaledb VERSION '<TS_PUENTE>'`,
   `timescaledb_pre_restore()`, `pg_restore --no-owner --role=user`, `timescaledb_post_restore()`.
5. `ALTER EXTENSION timescaledb UPDATE` (→ `TS_FINAL`), `ALTER EXTENSION postgis UPDATE`,
   `ALTER EXTENSION pgrouting UPDATE`; `CALL refresh_continuous_aggregate('resumen_trafico_ciclo', NULL, NULL)`;
   `ANALYZE`.
6. `python src/migrate.py` against the `Cluster` (port-forward to `geotracker-db-rw`) → "no pending migrations".
7. `verificar_bd.sh MODO=completo`.

- [ ] **Step 1: Run `--ensayo` without the script → fails** (file does not exist). Expected: FAIL.

- [ ] **Step 2: Implement the script** (steps 1–7 above).

- [ ] **Step 3: Run `--ensayo`**. Expected: verifier exit 0; timings of each step printed; `geotracker-db-0`
intact (`schema_migrations` and counts equal to before).

- [ ] **Step 4: Integration tests against the new version** — `db/cnpg/servidor_pruebas.sh arrancar` and
`DB_HOST=localhost DB_PORT=5433 PYTHONPATH=src pytest tests` (in the ETL image as `CLAUDE.md` describes, but
with `--network host` instead of `--network geotracker_red_trafico` to reach `localhost:5433`), plus
`PYTHONPATH=src:services/api pytest services/api/tests`. Expected: all pass; the `*_db.py` tests are **not** skipped.

- [ ] **Step 5: Run `--ensayo` a second time** (re-run). Expected: same as step 3.

- [ ] **Step 6: Commit**

```bash
git add deploy/k8s/scripts/actualizar_bd_pg17.sh
git commit -m "feat(db): upgrade to PG 17 via bridge TimescaleDB, rehearsed and verified (D46)"
```

---

### Task 6: Cut-over to the new database

**Files:**
- Create: `deploy/k8s/scripts/corte_bd_cnpg.sh`
- Modify: `deploy/k8s/base/04-pgbouncer.yaml` (host `geotracker-db-rw`), `deploy/k8s/base/09-osm-loader-job.yaml`
  (2× `DB_HOST`), `deploy/k8s/platform/kube-prometheus-stack/values.yaml` (`url:
  geotracker-db-rw.geotracker.svc.cluster.local:5432`, `postgresVersion: 1700`),
  `deploy/k8s/platform/grafana/alerts/bd.yaml` (PVC `geotracker-db-1`)

**Interfaces:**
- Consumes: `actualizar_bd_pg17.sh --final`, `verificar_bd.sh`.

- [ ] **Step 1: Cut-over guards (Review Focus 1)** — the script starts by suspending the `CronJob` and **aborts** if
`kubectl get jobs -n geotracker -l <CronJob label>` shows an active one; it waits for it to finish (max. 5 min).
Test: launch the script while a manual cycle is running (`kubectl create job --from=cronjob/geotracker-etl
prueba-corte`). Expected: it waits or aborts with a message; it does not dump halfway.

- [ ] **Step 2: Implement** — suspend → `actualizar_bd_pg17.sh --final` (dump with no cycle in progress) →
check that the last row of `ejecuciones_etl` matches in both → `kubectl apply -k` with this task's repoints
+ `helm upgrade` of kps with the values → `rollout restart` of `pgbouncer` and `geotracker-api` → resume the
`CronJob`.

- [ ] **Step 3: Run the cut-over**. Expected: script exit 0.

- [ ] **Step 4: Real route** — `kubectl port-forward svc/geotracker-api 8000:80` + the query from `CLAUDE.md`.
Expected: 200 and `trafico.vigente: true` after the next cycle.

- [ ] **Step 5: Grafana (Review Focus 3)** — `GET /api/datasources/uid/timescaledb/health` on the cluster's Grafana
→ `OK`; panel 1 shows data from the last hour; rules in Normal state.

- [ ] **Step 6: First cycle** — wait for the next `*/15`. Expected: new row in the new database's `ejecuciones_etl`
with `estado = 'ok'`; no new row in the old one.

- [ ] **Step 7: Commit** (D47: the cut-over, timings, window without ETL)

```bash
git add deploy/k8s docs/DECISIONES.md
git commit -m "feat(k8s): database cut-over to the CloudNativePG Cluster (PG 17) (D47)"
```

---

### Task 7: Resilience to the daily shutdown and to pod loss

**Files:**
- Modify: `docs/KUBERNETES.md` ("Observability: operations" → CNPG operations note)

- [ ] **Step 1: Delete the pod** — `kubectl delete pod geotracker-db-1 -n geotracker`. Expected: CNPG recreates it, the
`Cluster` returns to `healthy`, the API's `/health/ready` returns to 200 in < 2 min.

- [ ] **Step 2: Record** the measured time.

- [ ] **Step 3: Shutdown (Review Focus 4)** — `k3d cluster stop geotracker && k3d cluster start geotracker`.
Expected: `Cluster` `healthy` without intervention; the next cycle `ok`; the "ETL sin correr" alert resolves by itself.

- [ ] **Step 4: Document** operations commands (`kubectl get cluster`, `kubectl cnpg status` plugin if
installed at a pinned version, new SQL console) and commit.

```bash
git add docs/KUBERNETES.md
git commit -m "docs(k8s): operating the database on CloudNativePG (restart, console, status)"
```

---

### Task 8: Retire the `StatefulSet` and close the stage

**Files:**
- Delete: `deploy/k8s/base/03-database-statefulset.yaml`
- Modify: `deploy/k8s/base/kustomization.yaml`, `CLAUDE.md`, `docs/DATABASE.md`, `docs/KUBERNETES.md`
  (deployment diagram and "Database"), `docs/ARCHITECTURE.md`, `docs/DECISIONES.md` (D48),
  `db/Dockerfile` (header: "retired image; only for restoring backups prior to D47")

- [ ] **Step 1: Precondition** — at least 2 days of `ok` cycles on the new database and a readable `antes-pg17-*.dump`
backup. If not met, do not proceed.

- [ ] **Step 2: Retire** — remove 03 from `base`; `kubectl delete statefulset geotracker-db -n geotracker` and
`kubectl delete svc geotracker-db -n geotracker`; **keep** the PVC `datos-postgres-geotracker-db-0` (deleted by
hand after stage B, once there are off-site backups).

- [ ] **Step 3: Docs** — `CLAUDE.md` (pinned image warning → the new one; SQL console via `geotracker-db-rw`/pod
`geotracker-db-1`; integration tests with `db/cnpg/servidor_pruebas.sh` instead of the Compose database);
`DATABASE.md` (engine and extensions); `KUBERNETES.md` Mermaid diagrams updated and re-validated with
mermaid-cli (12/12 OK).

- [ ] **Step 4: Final verification** — `PYTHONPATH=src pytest tests` against `servidor_pruebas.sh`;
`kubectl get pods -n geotracker` without `geotracker-db-0`; API 200; cycle `ok`.

- [ ] **Step 5: Commit**

```bash
git add -A deploy/k8s db CLAUDE.md docs
git commit -m "chore(k8s): database StatefulSet retired; phase 2 stage A closed (D48)"
```
