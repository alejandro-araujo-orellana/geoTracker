# Decision log — geoTracker

What was decided, why, and which alternatives were discarded. One entry per decision, the most recent
at the end. Each entry cites the commit or migration where it was applied. If a decision is reverted,
it is not deleted: a new one that replaces it is added.

---

## D1 — Geometry in PostGIS instead of JSONB (2026-06)
**Decision:** geometries live in PostGIS `geometry` columns; TomTom's JSONB stays as a backup.
**Why:** Dijkstra routing (pgRouting) needs topology (`source/target`) and indexed spatial
lookups, impossible over JSONB.
**Where:** commit `1f8c662`; rules in `ARCHITECTURE.md` §4.

## D2 — Versioned schema with our own SQL migrations (2026-11-09)
**Decision:** idempotent `db/migrations/NNN_*.sql` + `src/migrate.py` (no Alembic).
**Why:** much of the schema had been created by hand and could not be reproduced. Plain SQL because almost
all the DDL is PostGIS/TimescaleDB/pgRouting-specific, which an ORM does not model well.
**Where:** commit `0f4d026` (P1).

## D3 — OSM edge identity by `(u, v, k)` (2026-11-09)
**Decision:** natural key of the osmnx graph; new `osm_id` with `blake2b`; old rows are adopted by geometry.
**Why:** Python's `hash()` is random per process and re-running the loader duplicated the network.
**Discarded:** deleting and reloading the network (it broke the `trafico_calles` history).
**Where:** commit `0f4d026` (P3/P4).

## D4 — Derived timestamps = timestamp of the source data (2026-11-09)
**Decision:** `trafico_calles.timestamp_calculo` is the most recent reading used, not `now()`.
**Why:** with `now()` the unique constraint never detects that a cycle was repeated on the same data.
**Where:** commit `e02d375` (P6).

## D5 — Retention: raw data never, derived data 1 year (2026-11-09)
**Decision:** compression after 7 days on the hypertables; 365-day retention only on `trafico_calles`.
**Why:** TomTom's raw data is the basis for historical hourly costs and cannot be
obtained again; derived data can be recomputed. 90 days would have deleted the whole history that same day.
**Where:** commit `bbf3aa5` (P8), migration 008.

## D6 — Single study area, the wide one (includes Soledad) (2026-11-09)
**Decision:** a single bbox in `src/zona.py`; OSM network expanded to that area (24 574 → 67 464 edges).
**Why:** 302 of 475 TomTom points fell outside the OSM network. Expanding the network (route coverage in the
outskirts) was preferred to trimming the points.
**Where:** commit `bbf3aa5` (P9).

## D7 — Monthly request pool (2026-11-09)
**Decision:** daily budget derived from the monthly quota, weighted by hour (peak/off-peak/night),
counting every HTTP attempt in `consumo_api`; deduplicated points (440 → 207).
**Why:** the Flow Segment Data quota is 20 000/month; the ETL used it up in hours.
**Discarded:** fully random schedules (at rush hour routing needs fresh, regular data).
**Where:** commit `671baef` (B.1, B.2), migration 009.

## D8 — Traffic Flow vector tiles as the main traffic source (2026-11-09)
**Decision:** live traffic comes from vector tiles (`relative`, zoom 14) and is stored **per OSM
edge**; Flow Segment Data stays as the secondary source (calibration/validation, within its pool).
**Why:** with 20 000 requests/month Segment Data cannot provide live coverage. Tiles have
200 000/month; 49 tiles cover the area (every 15 min ≈ 141 000/month) and the test measured 14 698 edges (98 %
of main roads) with a mean difference of 0.05 against Segment Data.
**Implies:**
- New tile extractor with its own pool in `consumo_api`.
- New per-edge readings table; tile geometry is not stored (it is stable and bulky).
- Tile → edge matching in PostGIS on every cycle, **directed** (see D9).
- Predictor: measured edges with their direct value; IDW only fills in the rest.
**Open risk:** confirm in TomTom's terms that storing data derived from tiles is allowed.
**Where:** test in commit `c60a55d`; implementation in the following commits.

## D9 — Directed tile → edge matching (2026-11-09)
**Decision:** a tile line is matched to the nearby OSM edge (< 12 m) with **the same bearing** (difference
< 35° over 360°), not merely parallel.
**Why:** in a downtown tile, two-way roads come as two parallel lines in opposite directions,
2–11 m apart, and in 37 of 50 tertiary pairs with different `traffic_level`: traffic is per direction, just
like OSM's directed edges. The original test (direction-agnostic) mixed both.

## D11 — Set-based tile → edge matching, with an index on the temporary table (2026-11-09)
**Decision:** the matching computes each edge's midpoint and bearing once in a temporary table that is
**indexed (GiST) and analyzed**, and joins by `ST_DWithin`; only nearby pairs compute bearings.
**Why:** the first version (`LATERAL ... ORDER BY <-> LIMIT 1`) and the second (JOIN without index or
statistics on the temporary table) took **more than 7 minutes** per pass: the planner did the
cross product of 67 k edges × 4 k segments (~280 M pairs) computing bearings. `EXPLAIN ANALYZE` showed
that with the index it drops to **1.3 s** (3.6 s including loading the segments), same result.
**Lesson:** every temporary table that takes part in a spatial JOIN needs an index + `ANALYZE`.

## D12 — Tile pass: all or nothing, and the main source does not block the secondary one (2026-11-09)
**Decision:** each cycle does the full tile pass (49) only if the daily budget allows it; a
tile failure is logged and the cycle continues with Segment Data; the predictor runs if any source brought data.
**Why:** a partial pass would leave areas unmeasured at the same instant. With 200 000/month the
daily budget (~6 000–8 000) covers the 96 cycles (4 704 tiles), so there is no need to weight by hour.

## D13 — Predictor v3: measured where there are tiles, IDW only for the rest (2026-11-09)
**Decision:**
- Edges measured in the latest tile pass (< 30 min) take their direct value:
  `congestion = 1 / traffic_level`, clipped to [1, 10] (`metodo_inferencia = 'TILE'`).
- The rest are inferred by IDW using as sources the centroids of the measured edges that are **not closed**
  (`'IDW_TILE'`). A closed road must not "paint" congestion onto its neighbours.
- If there is no recent pass, v2 (Segment Data) is used as a fallback (`'IDW_OSM'`).
- Congestion cap of 10 (previously 3): v2 clipped at 3 and underestimated real jams (Segment Data measured
  up to 8). Closed roads take the cap, but routing must **exclude** them using
  `fact_flujo_aristas.cerrada`, not just make them more expensive.
**Result (real cycle):** 15 039 measured edges + 52 425 inferred; only 1 % of streets without a nearby
source (previously 72.5 %). Measured distribution: 76 % near free flow (≥ 0.8), 18 severe jams (< 0.1)
and 61 closed roads; no level 0 without a closure flag (no "no data" disguised as a jam).
**Also:** `guardar_resultados` switches to batched SQLAlchemy INSERT with `RETURNING` (previously `iterrows` +
row-by-row `text()`: ~30 s; now ~3 s). `RETURNING` because with batched inserts `rowcount` only counts
the last batch. Full cycle: 37 s → 10.7 s.

## D14 — Routing in the database: cost view + `ruta_mas_rapida` function (2026-11-09)
**Decision:** the routing logic lives in PostgreSQL (migration 011): `v_costos_grafo` computes the cost in
seconds with the latest cycle's traffic and `ruta_mas_rapida(lat_o, lon_o, lat_d, lon_d)` snaps and runs
`pgr_dijkstra`. The API will only call the function.
**Why:** pgRouting needs the graph as SQL anyway; having it in a function makes it usable
from Grafana, psql or the API without duplicating logic, and testable against a real PostgreSQL.
**Adjustments to the `ROUTING.md` design:**
- Closed roads **excluded** (not just given a high cost): with a high cost Dijkstra would use them if there were no other way.
- `LEFT JOIN` to speeds with 20 km/h by default: with `JOIN` an edge of an unlisted type silently disappeared
  from the graph and could leave areas unreachable.
- OSM `maxspeed` only between 10 and 130 km/h (OSM data occasionally contains absurd values).
**Verified:** 7 integration tests with a synthetic graph; real route Centre → UASLP in 0.59 s over
~67 300 edges. With the 21:21 UTC traffic: 5.03 km / 10.4 min (the same route at free flow: 7.6 min).
**Correction:** commit `e2b4461` reported "5.16 km / 9.6 min at free flow". That was wrong: at that
moment there was already a traffic cycle from 12 min earlier (< 30 min), so the view used real traffic.

## D15 — ETL started only with `--no-deps` (2026-11-09)
**Decision:** the ETL is started with `docker compose up -d --build --no-deps etl_worker`.
**Why:** `trafico_db` and `grafana_bi` were created with Compose 1.29 (image `geotracker_timescaledb`); Compose
v5 names the image `geotracker-timescaledb` and, without `--no-deps`, **would recreate the database container**. The data
volume would persist, but there is no reason to risk it. Pending: migrate the containers to Compose v5 in
a controlled window (prior backup) and declare `restart: unless-stopped` on the ETL, so that a Docker restart
does not stop data collection.

## D16 — Thin routing API over the database (2026-11-09)
**Decision:** `GET /api/v1/rutas?origen=lat,lon&destino=lat,lon[&detalle=true]` (FastAPI, `services/api`). The API
only validates, calls `ruta_mas_rapida()` and formats: distance, duration with traffic, free-flow duration of the
same route, traffic delay, traffic freshness, streets in order and GeoJSON geometry.
**Why this way:**
- The free-flow time was extracted to the `v_tiempo_libre` view (migration 012) and `v_costos_grafo` uses it: a single
  speed formula for routing and for the API.
- Coverage by distance to the network (> 300 m from the nearest node → 422), not by bbox: it does not duplicate `zona.py` in another
  service and reflects the network actually loaded.
- Explicit errors: 422 (format, out of coverage, origin = destination), 404 (no route, e.g. wrong-way only).
- Uses the `app/rutas/` folder that already existed in the skeleton; `app/api/v1` only groups routers.
**Verified:** 12 tests against a real PostgreSQL (temporary database); live, Centre → UASLP in 0.29 s.

## D17 — Containers on Compose v5, pinned images and `restart` (2026-11-09)
**Decision:**
- `docker-compose.yml` for Compose v2+ (`name: geotracker`, no `version`), credentials only from `.env`,
  `restart: unless-stopped` on the 4 services, database healthcheck and `depends_on: service_healthy`.
- **Pinned images:** database `geotracker-timescaledb:pg15.2-ts2.10.2` (the existing image, re-tagged) with
  `db/Dockerfile` anchored by digest to the 2023 base and pgRouting `4.0.1`; Grafana `13.0.2` (the local one).
- The ETL no longer mounts the repo (`.:/app`): it runs the code in its image.
**Why:**
- `pg15-latest` today ships TimescaleDB versions that cannot load a database with extension 2.10.2 without
  `ALTER EXTENSION ... UPDATE`; rebuilding the database "by accident" could leave it unable to start. Upgrading
  PostgreSQL/TimescaleDB/pgRouting becomes a migration of its own (backup + test).
- `restart` prevents a Docker/WSL restart from stopping data collection.
- Without the bind mount, what runs is exactly what was built (a requirement for Kubernetes in the next milestone).
**Migration performed:** prior backup (`vialidades_slp_20261109_193205_pre_compose_v5.dump`); same volumes
(`geotracker_datos_postgres`, `geotracker_grafana_data`); identical extensions; no count decreased (the new ETL
added its first cycle). Replaces the restriction of D15: a normal `docker compose up -d` can now be used.

## D18 — Tools for the Kubernetes milestone (preliminary, 2026-11-09)
**Decision:** **Terraform** (infrastructure: cluster, network, registry, backups and the database if it stays outside the cluster)
and **Ansible** (host/VM configuration and operational procedures such as the TimescaleDB upgrade) will be used.
**Prometheus** (+ Alertmanager) is the observability option to evaluate. **Puppet is discarded.** CI/CD (Jenkins or
GitHub Actions) is still open.
**Why:** each tool covers a different layer and they do not clash as long as each manages a single domain
(Terraform does not deploy the apps; Ansible does not deploy containers; a single deployment path). Puppet overlaps
with the container model, requires a server and agents, and would conflict with Kubernetes on the nodes;
Ansible covers host preparation without agents.
**Where:** full analysis in `docs/KUBERNETES.md` › Complementary tools. To be confirmed or adjusted in the
milestone session.

## D19 — Cloud: Azure (AKS), after the local environment (2026-11-12)
**Decision:** the order is local first (phase 1) and cloud afterwards (phase 2). The cloud provider is **Azure (AKS)**,
with the $100 USD free credit as the budget. Image registry: Azure Container Registry or GHCR.
**Why:** learning Kubernetes locally does not consume credit; the Azure credit covers phase 2 if the cluster is
shut down when not in use. The managed database (option C) was discarded because no service offers the pinned combination
PostgreSQL 15.2 + TimescaleDB 2.10.2 + PostGIS + pgRouting (D17): in the cloud the database will run on CloudNativePG (option B).
**Pending:** measure usage against the credit in phase 2; `overlays/nube` and `deploy/terraform/` do not exist yet.
**Where:** `docs/KUBERNETES.md` › Phase 2; base manifests in commit `8461992`.

## D20 — Kustomize (base + overlays), not Helm (2026-11-12)
**Decision:** the manifests are managed with **Kustomize**: `deploy/k8s/base` (the shared part) + `overlays/local` and,
in phase 2, `overlays/nube`, which only patch what differs (replicas, storage, Ingress, secrets).
**Why:** it is built into `kubectl` (`apply -k`), it is plain YAML with no templating language, and for a single
project with two environments Helm's chart packaging/versioning is not needed. Helm is kept for third-party
components (e.g. `kube-prometheus-stack`, phase 3).
**Consequence:** `03b-database-cnpg.yaml` lives in `base/` but outside `kustomization.yaml` until the
operator is installed; the A → B switch is swapping that file in the list.
**Where:** commit `8461992` (`deploy/k8s/base/kustomization.yaml`, `deploy/k8s/overlays/local/kustomization.yaml`).

## D21 — Local cluster with k3d (2026-11-12)
**Decision:** the development cluster is **k3d** (k3s inside Docker containers): 1 server + 2 agents, cluster
`geotracker`. Images are loaded with `k3d image import` (no registry) and `imagePullPolicy: IfNotPresent`.
**Why:** it runs on the Docker that Compose already uses, without VMs; it is recreated with one command; it ships Traefik (Ingress) and the
`local-path` provisioner (PVC) out of the box. Several nodes allow practising replica spreading (D22).
Terraform/Ansible are reserved for the cloud.
**Watch out:** Compose names the ETL image `geotracker-etl_worker`; the manifests use `geotracker-etl`, so it must be
re-tagged before importing it (table in `docs/KUBERNETES.md` › Images).
**Where:** commit `8461992`.

## D22 — Own PgBouncer, PodDisruptionBudget and spreading across nodes (2026-11-12)
**Decision:** three additions to the original design, for the milestone's two goals (redundancy and multi-user):
- **PgBouncer** as its own `Deployment` (2 replicas in base, 1 locally), `transaction` mode, 20 real connections to
  Postgres. All database traffic goes through the `pgbouncer:6432` `Service` (`DB_HOST` in the ConfigMap).
- API **PodDisruptionBudget** with `minAvailable: 1`: a node drain never takes the API to zero.
- **topologySpreadConstraints** by `kubernetes.io/hostname` (`ScheduleAnyway`): API replicas on different
  nodes; in the cloud it can be hardened to `DoNotSchedule`.
**Why:** each API replica (2 uvicorn workers × pool of 5 + 5) and the ETL open their own pool; with 3 replicas
they approach `max_connections = 100`, and Postgres creates one process per connection. Without a PDB or spreading, the redundancy of 3
replicas is only theoretical: they can go down together during a drain or a node failure.
**Notes:** the `bitnami/pgbouncer` image is no longer on Docker Hub; `bitnamilegacy/pgbouncer:1.22.1` is used (no
updates; review in phase 2 or use CloudNativePG's `Pooler`). Locally, with 1 replica, the PDB blocks
`kubectl drain` (the only pod cannot be evicted): expected.
**Where:** commit `8461992` (`04-pgbouncer.yaml`, `05-api.yaml`).

## D23 — Non-public API: token access (2026-11-12)
**Decision:** the API is not published (neither in stores nor as an open service). In the cloud, access is limited to
own devices with a **token in a header** validated by the API itself (401 if missing or mismatched) and,
optionally, IP restriction and TLS (cert-manager) on the Ingress.
**Why:** keeps the project within D8 (data derived from TomTom for own use/research, no
public redistribution). The token in the application is portable across Ingress controllers (Traefik locally,
whichever is used on AKS), unlike each controller's authentication annotations.
**Status:** implemented before phase 2 to test it locally and on Compose — see D26. TLS in the cloud is still missing.
→ The optional IP restriction is discarded in D49.
**Where:** `08-ingress.yaml` in commit `8461992`; `docs/KUBERNETES.md` › Phase 2.

## D24 — ETL in one-cycle mode (`--una-vez`) for the CronJob (2026-11-12)
**Decision:** `python src/main.py --una-vez` runs one `ciclo_etl` and exits; without the flag the Compose
daemon continues (migrations on startup + a cycle every 15 min), unchanged.
**Details:**
- `--una-vez` **does not apply migrations**: in Kubernetes they are applied once by the `geotracker-migrate` `Job`;
  doing it every 15 min from the CronJob would only open DDL races.
- Exit 1 if the cycle raises an exception (the Job is marked failed, with its log); exit 0 otherwise, including
  "no data" due to lack of quota and a failed tile pass that the cycle already absorbs (D12). A total TomTom
  failure is detected through metrics (phase 3), not the exit code.
- `main(argv)` is testable: 3 new tests in `tests/test_main.py`.

## D25 — Database volume in Kubernetes: mount PGDATA's parent + `fsGroup` (2026-11-12)
**Decision:** the PVC is mounted at `/home/postgres/pgdata` and `PGDATA=/home/postgres/pgdata/data` (the same data
path as Compose and the image). The pod runs as `1000:1000` with `fsGroup: 1000` and
`fsGroupChangePolicy: OnRootMismatch`.
**Why:** mounting the PVC directly on `PGDATA` (like Compose) **fails**: a PVC arrives owned by root and `initdb`
cannot `chmod 0700` ("could not change permissions ... Operation not permitted"). Compose does not have the
problem because a named Docker volume copies the owner of the image's directory. Tested with the pinned image
on a root:0777 directory (like k3d's `local-path`): mounting on `PGDATA` → fails; mounting on the parent →
starts. On Azure Disk the disk arrives root:0755 with `lost+found`, so `fsGroup` is also needed for
`postgres` to be able to create `data/`. `local-path` ignores `fsGroup` (it is hostPath), but its 0777 is enough.

## D26 — Access token in the API: `X-API-Key` (2026-11-12)
**Decision:** all of `/api/v1/*` requires the `X-API-Key` header with one of the tokens in `API_TOKENS` (comma-separated
list, one per device); if missing or mismatched → 401 with `WWW-Authenticate: ApiKey`. `/health` and `/docs` remain
open. Implements D23 right away, on Compose and on Kubernetes.
**Details:**
- **Fails closed:** without tokens, or with any shorter than 32 characters, the configuration is not built and the API does not
  start. Only `API_AUTH_DESACTIVADA=true` leaves it open (development; it warns in the log).
- Comparison with `secrets.compare_digest` against **all** tokens (without stopping at the first match).
  The received token is never logged.
- The dependency goes in `include_router(..., dependencies=[...])`: any new router under `/api/v1` is
  protected without having to remember to add it. `APIKeyHeader` makes `/docs` show the "Authorize" button.
- `/health` is open because Kubernetes probes and the Compose healthcheck call it, and they send no token.
  `/docs` only exposes the schema, not data; it can be closed in the cloud if desired.
- Revoking a device = removing its token from the list and restarting the API.
**Verified:** 11 new tests (`services/api/tests/test_seguridad.py`, no database) + the 12 route tests with the header;
live on Compose: no token 401, wrong token 401, correct 200, `/health` 200.
**Usage:** `curl -H "X-API-Key: $TOKEN" 'localhost:8000/api/v1/rutas?...'` (the token is in `.env`).

## D27 — Kubernetes secrets outside `base/`: `secretGenerator` from an unversioned file (2026-11-12)
**Decision:** `base/02-secret.yaml` is removed. Locally, the `geotracker-secret` `Secret` is generated by Kustomize from
`overlays/local/secret.env` (in `.gitignore`; versioned template `secret.env.example`), with
`disableNameSuffixHash` to keep a fixed name. In the cloud it will come from Sealed/External Secrets (phase 2).
**Why:** a `Secret` in `base/` only had two bad outcomes: with empty values, every `kubectl apply -k`
overwrote the real secret created by command; with the real values, the file (already versioned in `8461992`) ended up
in git and on GitHub. The generator reads a file git does not see, and `apply -k` is still a single command.
**Consequences:** the overlay declares its own `namespace` (the base's one does not reach what the overlay generates).
With a fixed name, changing a value does not restart the pods: `kubectl rollout restart`. The values that were in
`02-secret.yaml` were moved to `secret.env` and verified to match `.env` and the rendered `Secret`.

## D28 — First deployment on k3d: service links, suspended CronJob and Ingress (2026-11-13)
**Decisions:**
- **PgBouncer with `enableServiceLinks: false` and explicit `PGBOUNCER_PORT`.** Kubernetes injects variables for every
  Service in the namespace; the `pgbouncer` Service generates `PGBOUNCER_PORT=tcp://10.43.x.x:6432`, which clashes with the
  Bitnami image's variable of the same name ("value is not an integer") → `CrashLoopBackOff`, and in cascade the
  migrations and ETL failed (`Connection refused`).
- **ETL CronJob suspended in `overlays/local`** (`suspend: true` patch, declarative so that `apply -k` does not
  re-enable it). While the Compose ETL runs, both would spend the same TomTom key with separate `consumo_api`
  pools (~282 000 of 200 000 tiles/month), and the cluster's database does not have the OSM network yet. Removed at the cut-over
  (phase 4, stopping the Compose ETL first). Manual test: `kubectl create job --from=cronjob/geotracker-etl`.
- **Ingress with `spec.ingressClassName: traefik`** instead of the deprecated `kubernetes.io/ingress.class` annotation.
  On AKS the class will be different and `overlays/nube` patches it.
**Verified:** pods `Running`, migrations Job `Complete` (001–012), `/health` ok through PgBouncer, `apply -k` without
warnings; via `port-forward`: no token 401, with token 503 "La red vial no está cargada" (expected: OSM still has to be loaded).
**Operational note:** a failed `Job` is not re-run: `kubectl delete job geotracker-migrate` and `apply -k` again.

## D29 — OSM network as a Kubernetes `Job` and cut-over from the Compose ETL to the `CronJob` (2026-11-13)
**Decisions:**
- **`base/09-osm-loader-job.yaml`**: `scripts/osmnx_loader.py` runs as a `Job` with the ETL image (the
  `Dockerfile` now copies `scripts/`; a `.dockerignore` is added so that `.env`, `cache/` and `.git` are not put into the
  context). An `initContainer` waits for `schema_migrations` to reach 005, so a single `apply -k` works even if
  the migrations `Job` has not finished. It goes straight to `geotracker-db:5432`, not through PgBouncer: it is a bulk
  load in a single transaction and gains nothing from the transaction pool.
- **osmnx cache on a PVC (`cache-osmnx`, 1 Gi) seeded from the host's `./cache`.** The public Overpass
  (`overpass-api.de`) rejects automated clients: it answers with a "run your own instance" notice instead of
  JSON (the 3 attempts of the first `Job` failed like that). With the cache (same bbox and same osmnx version) the load
  took 27 s and produced the same network as Compose: 67 464 edges, 28 008 nodes, 1 component, 0 edges without vertex.
  Seeding: `deploy/k8s/tools/sembrar-cache-osmnx.yaml` + `kubectl cp`. *Updating* the network will need an
  Overpass that accepts the load (own instance) or running the loader on the host and seeding the cache again.
- **ETL: the Compose one is stopped and the `CronJob` is activated** (the `suspend` patch from D28 is removed). The
  `etl_worker` service stays behind the `etl-compose` profile, so that a `docker compose up -d` does not revive it and double the
  quota; starting it requires `--profile etl-compose` and suspending the `CronJob` first.
- **The month's `consumo_api` is copied to the cluster's database** before the first cycle. Without this the `CronJob` would start from
  zero and believe it had the whole monthly quota (Compose had already used 8 282 tiles and 1 839 Segment Data in November).
**Verified:** first scheduled cycle (17:15 UTC) `Complete` in 16 s: budget "mes 7106/200000 antes de hoy",
49/49 tiles, 14 997 measured edges + 52 467 by IDW in `trafico_calles`. `GET /api/v1/rutas` with a token returns
a real route.
**Consequence — split data:** the history (since 2026-11-09) stays in the Compose database and the new cycles go
to the cluster's. Grafana still reads the Compose one, so its heatmap is frozen at the last Compose cycle
(17:02 UTC). Pending for phase 4: bring the history into the cluster (`pg_dump`/`pg_restore` of data) and
deploy Grafana in the cluster or point it at its database.

## D30 — OSM network from a `.osm.pbf` extract instead of Overpass (2026-11-13)
**Context:** the public Overpass rejects automated clients (D29). The cache seeded from the host worked for
the initial load, but froze the network at the cache's date and did not allow updating it.
**Decision:** `osmnx_loader.py` reads a `.osm.pbf` extract by default. Source: `OSM_PBF_URL`, by default the
state of San Luis Potosí on openstreetmap.fr (~15 MB, regenerated daily; alternative: all of Mexico on
Geofabrik). Done by `scripts/fuente_pbf.py` with pyosmium (`osmium==4.0.2`). Only osmnx's *download* is
replaced: the `.pbf` is converted into the same JSON response Overpass would give and `ox.graph_from_bbox` does the rest
unchanged (500 m buffer, truncation, largest component, simplification). The selection replicates osmnx's
query:
- the `drive` filter is read from osmnx itself (`_get_network_filter`) and evaluated with Overpass semantics;
- ways with any node inside the polygon are taken, with all their nodes;
- elements are sorted by id and coordinates have 7 decimals.
It uses osmnx internals pinned by version (`osmnx==2.1.0`); when upgrading osmnx the validation must be repeated.
`--fuente overpass` keeps the previous path (Overpass or its cache) and `--pbf file` uses a local extract.
**Validation:** same bbox, graph from the Overpass cache (2026-11-09) against graph from the extract
(data 2026-11-13T00:49Z): 28 162 nodes and 67 914 edges in both, 0 differences in `(u,v,k)` and 0 in
`osmid`/`highway`/`name`/`oneway`/`maxspeed`/`length`/geometry. On k3d the `Job` took 53 s: 0 new edges,
67 464 updated, 0 inactive, 0 edges without vertex, 1 component.
**Consequences:** the `Job` no longer needs the `cache-osmnx` PVC or the seeding pod (they are removed); it uses an
`emptyDir` and needs internet egress to the extract's server. Updating the network = deleting the `Job` and
running `apply -k` again. Tests: `tests/test_fuente_pbf.py`.

## D31 — Observability: Prometheus for operations, TimescaleDB for business, a single Grafana (2026-11-13)
**Context:** phase 3 of `KUBERNETES.md`. Grafana is still on Compose reading the Compose database (frozen since the cut-over of
D29), without provisioning (dashboards and datasource only exist in the `geotracker_grafana_data` volume), and there are no
metrics or alerts: on 2026-11-13 the ETL went ~4 h without running (computer off) and nothing warned about it.
**Decisions** (close questions 9 and 10 of `KUBERNETES.md`):
- **Two sources, separated by data type.** Business data (congestion, heatmap, ETAs) stays in TimescaleDB;
  operations (API latency and errors, ETL success, restarts, disk, quota) go to Prometheus. Congestion
  is **not** exported to Prometheus: ~67 k `osm_id`s would be ~67 k series and the history already lives in TimescaleDB.
- **`kube-prometheus-stack` via Helm with versioned `values`** (`deploy/k8s/platform/`), reduced locally (one
  replica, 7-day retention). It does not contradict D20: Kustomize stays in charge of the geoTracker apps; the chart is
  platform, like Traefik. In the cloud Terraform installs it (D18).
- **Single Grafana: the chart's**, with TimescaleDB + Prometheus datasources and dashboards as ConfigMaps (provisioning
  sidecar). The Compose dashboards are exported to JSON and versioned. The Compose one is retired after
  phase 4 (history migration).
- **No Pushgateway.** The ETL lives ~20 s and its state already goes to the database (D7). Job health: `kube-state-metrics`
  (`kube_cronjob_status_last_successful_time`, included in the chart). Cycle detail: the `ejecuciones_etl` table
  (one row per cycle), graphed from TimescaleDB and surviving Prometheus' retention.
- **Grafana enters the database with a read-only role** (`grafana_ro`), straight to `geotracker-db:5432`: not with the
  schema owner, and not through PgBouncer in transaction mode (prepared statements).
- **Alerts: a single set of rules, a notification engine per environment.** The rules are written once as
  provisioned Grafana rules (they can query Prometheus *and* SQL: quota in `consumo_api`, freshness of
  `trafico_calles`). Locally: Grafana's internal Alertmanager notifies (Grafana Alerting); the chart's
  Alertmanager is disabled to save memory. Cloud (AKS): the chart's Alertmanager is active; Grafana sends it its
  alerts as an external Alertmanager, and the chart's default rules (nodes, PVC,
  restarts) arrive there too. The difference between environments lives in the overlays/`values` (`alertmanager.enabled` and the destination of
  Grafana's alerts), not in the rules.
**Why not two sets of rules** (Grafana locally, `PrometheusRule` in the cloud): they diverge over time, and what
is tested locally stops being what runs in the cloud. Besides, Prometheus cannot query SQL: the
quota and freshness alerts would require an extra exporter just for the cloud.
**Pending verification when writing `overlays/nube`:** sending Grafana alerts to an external Alertmanager
(Alertmanager-type datasource + Alertmanager choice in Grafana's configuration) with the Grafana version the
chart ships at that time.

## D32 — Separate probes (`/health/live`, `/health/ready`) and `/metrics` in the API (2026-11-13)
**Context:** Kubernetes probes only look at the HTTP code, and `/health` returns **200 always**, even with
`"status": "degraded"`: both probes pointed there and never detected a downed database.
**Decisions:**
- **`/health/live`** (liveness) does not touch the database: if the database goes down, restarting the API fixes nothing and would restart all
  replicas at once. **`/health/ready`** (readiness) queries the database and returns **503** on failure: the Service takes
  the replica out of rotation without killing it. `/health` stays as it was (informational). The Compose healthcheck switches
  to `/health/ready`.
- **`/metrics`** with `prometheus-fastapi-instrumentator==7.1.0` + `prometheus-client==0.21.1` (8.x requires
  Starlette ≥ 1.0, incompatible with `fastapi==0.115.6`). No token, same as `/health`. Labels by route
  template (never by the URL with its query) and excludes the probes and the scrape itself.
- **One uvicorn worker per container** (previously 2): with several processes each has its own counters and each
  scrape would see a random one. Scaling is done with Deployment replicas; the routes are `def` (threadpool), so one
  worker serves several requests while they wait on the database.
- **The Ingress only publishes `/api`, `/docs` and `/openapi.json`** (previously the whole `/`). `/metrics` and `/health/*`
  stay internal: Prometheus and the kubelet reach them via the Service and the pod.
- The `ServiceMonitor` is added with the stack (D31): its CRD does not exist in the cluster yet.
**Verified (k3d, 2026-11-13):** API tests 29/29 (new `tests/test_observabilidad.py`; routing tests against a real
database). Via the Service: `/health/live`, `/health/ready`, `/metrics` and the route with token → 200, and `http_requests_total`
labelled `handler="/api/v1/rutas"`. Via the Ingress: `/metrics`, `/health`, `/health/ready` → 404; `/docs`,
`/openapi.json` and the route with token → 200; no token → 401. With PgBouncer scaled to 0: `/health/ready` 503,
`/health/live` 200, the replica goes `notReady` in the endpoints and **0 restarts**; once restored it goes back to
`ready` by itself.

## D33 — Grafana in the cluster: stack installed, `grafana_ro` role and dashboards as code (2026-11-14)
**Context:** implementation of D31 on k3d. The dashboard exported from the Compose Grafana ("HeatMap SLP Traffic")
comes in the new v2 format (`dashboard.grafana.app/v2`) and points to a datasource that only exists on Compose.
**Decisions:**
- **`kube-prometheus-stack` 93.3.0** (`deploy/k8s/platform/kube-prometheus-stack/`): shared `values.yaml` +
  `values-local.yaml`. Release `kps` in the `monitoring` namespace. Helm runs from the `alpine/helm:3.17.3` image
  (nothing installed on the host). Locally: no Alertmanager, none of the control plane targets k3s embeds
  (etcd, scheduler, controller-manager, kube-proxy, always "down"), Prometheus with 7 days on a 5 Gi
  PVC.
- **Grafana pinned to `grafana/grafana-oss:13.0.2`** (the chart ships 13.3.1): it is the version the v2
  dashboard came from and the one with which file provisioning was confirmed to load it, and the same image as Compose.
  Upgrading it is an explicit decision, reviewing the dashboards.
- **`grafana_ro` role** (migration 013): only `SELECT`, default privileges for future tables, `statement_timeout`
  30 s and at most 10 connections. The migration creates it without login. The password does not go in git: `migrate.py` assigns it
  if it receives `GRAFANA_DB_PASSWORD`, which comes from the Secret. The datasource goes straight to `geotracker-db:5432`, not through
  PgBouncer.
- **Secrets:** `overlays/local/grafana.env` (outside git; template `grafana.env.example`) generates `grafana-secret`
  in both namespaces from **a single file**: the password assigned by the migrations Job (`geotracker`) and the
  one Grafana uses (`monitoring`) cannot drift apart. The overlay stops declaring a global `namespace:` (it spans
  two namespaces); each generator declares its own. It was verified that the render of the existing objects does not change.
- **Dashboards as code:** `deploy/k8s/platform/kustomization.yaml` generates one ConfigMap per JSON, with the label
  `grafana_dashboard=1` and the "geoTracker" folder, without a hash suffix (with a hash, `apply` would leave the previous version and
  Grafana would see the same uid twice). The API's `ServiceMonitor` also lives there. The overlay includes
  `../../platform`.
- **Changes to the exported dashboard** (the v2 format and uid `adrdrr6` are kept): the three queries point to the
  provisioned `timescaledb` datasource; the Compose instance metadata is removed; the maps are centred
  on SLP. **Panel 3 ("heatmap") read `trafico_inferido` (`IDW_GRID`)**, the legacy table of P10, which is no longer
  written and does not exist in the cluster. That map on Compose shows old data from long before the D29 cut-over.
  It now reads the latest `trafico_calles` cycle, aggregated into 0.002° cells (~200 m): 67 464 streets → ~3.3 k
  points, ~0.33 s.
**Operational trap:** `k3d image import` with **two images in the same command** reported success and left the
previous `geotracker-etl` on the nodes: the migrations Job ran without 013 ("Esquema al día"). Imported alone, it was
replaced. Import one image per command and check the date with
`docker exec k3d-geotracker-agent-0 crictl inspecti docker.io/library/<image>`.
**Verified (k3d, 2026-11-14):**
- **Tests:** ETL 164/164, with new `tests/test_rol_grafana_db.py` against a real database (reads hypertables, does not write, reads
  future tables, password rotation, 30 s cut-off); API 29/29.
- **Grafana 13.0.2:** the TimescaleDB (as `grafana_ro`) and Prometheus datasources pass the health check. The dashboard
  appears in the geoTracker folder and its three queries, run by Grafana, return 19, 207 and 3 333 rows.
  A `DELETE` as `grafana_ro` → `permission denied`.
- **Prometheus:** `up{job="geotracker-api"} = 1`, `http_requests_total` per route, and the ETL's
  `kube_cronjob_status_last_successful_time` available: the basis of the "ETL sin correr" alert.
- **Stack memory:** Grafana ~330 MiB, Prometheus ~285 MiB and the rest <70 MiB.
**Pending:** Grafana logs an internal `level=error` once on load ("[SHOULD NOT HAPPEN] failed to update
managedFields") about the v2 schema; the dashboard works. Review when upgrading.

## D34 — Static IPs for the k3d nodes (2026-11-16)
**Decision:** the `geotracker` cluster's containers are reconnected to the `k3d-geotracker` network with a fixed IP
(`server-0` .2, `serverlb` .3, `agent-0` .4, `agent-1` .5).
**Why:** after two days off, `k3d cluster start` failed: Docker assigned the IPs by start-up order and
`agent-1` (registered as .5) got .3; k3s shut down in a loop (`failed to find interface with specified node
ip`). Deleting the `Node` object so it re-registers was discarded: it fixes it once, but the problem returns on the
next restart. With the IP in the endpoint configuration, Docker always honours it.
**Consequence:** if the cluster is recreated with `k3d cluster create`, repeat the procedure in
`docs/KUBERNETES.md` ("Observability: operations"). Does not apply to Azure (phase 2).

## D35 — Compose Grafana stopped (2026-11-16)
**Decision:** the Compose `grafana` service moves to the `grafana-compose` profile (like the ETL in D29) and the
`grafana_bi` container is removed; the `geotracker_grafana_data` volume is kept.
**Why:** it reads the Compose database, with no new data since 2026-11-13 17:02 UTC (D29 cut-over): with the default
range (last 6 h) its panels come out empty, and with two Grafanas with different credentials (`.env` vs
`grafana.env`) it was confusing which one was current. The cluster's (D33) is the only one.
**Using it again:** `docker compose --profile grafana-compose up -d grafana`.

## D36 — Geomap base map: OpenStreetMap instead of CARTO (2026-11-16)
**Decision:** the dashboard's two Geomap panels use `basemap.type = "osm-standard"` (tiles from
tile.openstreetmap.org) instead of `"default"`.
**Why:** `"default"` uses CARTO's basemaps, which now require an API key: the map showed "API KEY REQUIRED
carto.com/basemap/apikey" and only the points were visible. OSM requires no key; its tile usage policy allows
this volume (internal use, a handful of users). In production with more traffic: a provider with a key or own
tiles.
**Verified:** screenshot with headless Chromium (Playwright, locale `es-MX`): the tiles load and the 3 queries
respond. Note: without a locale, headless Chromium does not start Grafana 13 (`RangeError: Incorrect locale information
provided` in `Intl.NumberFormat`); it is the test browser's issue, not the server's.

## D37 — Panel 2: streets measured by tiles coloured by congestion (2026-11-16)
**Decision:** the "GeoMap Traffic" panel becomes "Tráfico medido por calle (último ciclo)". It reads `trafico_calles` with
`metodo_inferencia = 'TILE'` from the range's latest cycle (~15 k edges, 1 223 km) instead of the latest
Segment Data reading per point. Each edge is densified into points every 30 m (`ST_LineInterpolatePoints` in UTM 14N) and the
markers (3 px, opacity 0.9) are coloured with thresholds 1.3 / 1.7 / 2.5 and purple ≥ 9.99 (closed). Ascending
order so the congested ones are drawn on top (two-way streets are two edges).
**Why:** the previous panel did not convey traffic: fixed yellow colour, 5 px and opacity 0.4 over a map
of yellow streets, and Segment Data is already a secondary source (1–4 of 207 points per cycle; there were readings up to
2.5 days old). Geomap does not draw one line per segment (the "route" layer joins all the frame's points) and the
GeoJSON layer only accepts static files; densifying into points gives continuous strokes at zoom 12–15 without plugins.
**Cost:** ~38 k points, ~0.3 s in the database and ~3.4 MB per panel load. If it gets heavy, raise the step to 50 m or filter
`tipo_via`. Inferred edges (`IDW_TILE`) remain in panel 3.
**Verified:** Chromium screenshots (full view and zoom on the centre): continuous coloured strokes, legend with the
5 ranges and tooltip with street, road type and cycle time.

## D38 — Panel 1 as a time series over a continuous aggregate (2026-11-17)
**Decision:** panel 1 ("HeatMap SLP Traffic", heatmap over Segment Data) becomes "Congestión por ciclo (calles
medidas)", a time series with the average congestion (left axis) and the % of edges with congestion ≥ 1.3 (right
axis, dashed). It reads the continuous aggregate `resumen_trafico_ciclo` (migration 014). Gaps > 30 min are not joined
(`insertNulls`) so that lost cycles are visible.
**Why:** Segment Data measures 1–4 points per cycle (5 rows in 6 h): the series did not represent the city. The
~15 k edges measured by tiles do, but aggregating `trafico_calles` (67 k rows per cycle) on each load took ~8 s
with only 90 cycles and grows linearly with the history; with the aggregate, ~30 ms. Only `TILE` edges: `IDW_TILE` ones
are interpolated and dilute the signal. The % complements the average, which with 15 k mostly free-flowing edges
barely moves (1.00–1.18).
**Discarded alternatives:** a summary table written by `predictor.py` (more code in the ETL and another write path
to keep idempotent); `migrate.py` with migrations outside a transaction (unnecessary: `WITH NO DATA`
allows creating the aggregate inside one and the policy does the initial refresh).
**Verified:** `tests/test_resumen_trafico_db.py` (4 tests: `TILE` only, thresholds, same live and materialized,
reading as `grafana_ro`, policy registered); ETL suite 168/168. Migration applied on k3d by the Job; screenshot
of the panel with 2 days of data.

## D39 — PostgreSQL memory sized to the pod limit (2026-11-17)
**Decision:** the database StatefulSet passes `args` to `postgres` with `shared_buffers=256MB`,
`effective_cache_size=768MB`, `work_mem=8MB`, `maintenance_work_mem=64MB`, `autovacuum_max_workers=3`,
`max_parallel_workers_per_gather=1` and `max_parallel_workers=2`, in line with `limits.memory: 1Gi`.
**Why:** when loading the dashboard (D37/D38) the database pod died with **OOMKilled** (exit 137) and started with
WAL recovery. The volume's `postgresql.conf` was generated at `initdb` with values for the whole host
(16 CPUs, ~8 GB): `shared_buffers` 1955 MB, `maintenance_work_mem` 1 GB × 10 autovacuum workers and 8 parallel
workers per query, far above the 1 GiB limit. It had already accumulated 3 restarts. With `-c` on the command
line the file is overridden without touching the volume and stays versioned; `ALTER SYSTEM` would have left the
configuration inside the PVC, outside git.
**Verified:** `pg_settings` shows the 7 values with `source = command line`; two full dashboard loads
(3 panels, 2 days) without a restart; maximum working set 688 MiB (cgroup peak 877 MiB, includes page cache).
**Pending:** if the real peak grows with the history, raise the limit to 1.5 Gi rather than lowering `shared_buffers`.
On CloudNativePG (phase 2) these values go in `spec.postgresql.parameters`.

## D40 — "ETL sin correr" alert as code, ETL status panel and panel 3 weighting (2026-11-17)
**Decision:**
- **Alert:** a Grafana rule provisioned from `deploy/k8s/platform/grafana/alerts/etl.yaml` (ConfigMap with
  `grafana_alert=1`; alerts sidecar enabled in `values.yaml`). It queries Prometheus:
  `(time() - max(kube_cronjob_status_last_successful_time{cronjob="geotracker-etl"})) / 60 > 45`, evaluated every
  1 min with `for: 2m`. `noDataState: Alerting`: without the series (kube-state-metrics down or the CronJob deleted) it is not
  possible to claim the ETL is running.
- **Panel 4:** "ETL: minutos desde el último ciclo exitoso", the same expression as a stat (green < 20, orange < 45,
  red ≥ 45).
- **Panel 3:** the heatmap weight is `least(greatest(congestion − 1, 0), 1.5)` with a fixed 0–1.5 range. With raw
  congestion (≥ 1) all cells weighed almost the same and the map showed street density, not traffic.
**Why 45 min and `for: 2m`:** 3 missed `*/15` cycles (threshold from "Initial alerts" in `KUBERNETES.md`): a
slow Job or a skipped cycle does not alert; a real outage does. The computer is switched off daily: on start-up, the alert
fires ~2 min after Grafana starts and resolves after the first cycle. That is correct (those hours have no
data) and works as a daily test that the metric → rule → state chain works.
**No contact point yet:** the default one is email without SMTP; alerts are only visible in Grafana
(Alerting → Alert rules). Choosing a channel (email with SMTP, Telegram, Discord, ntfy…) is the next decision.
**Verified (k3d):** the rule loads with `provenance: file` in the geoTracker folder and evaluates without error (state
Normal, 1–2 min since the last cycle). Firing path: a temporary copy with threshold 0 went to **Alerting** with
the rendered description ("…hace 1 min…") and was deleted. Dashboard screenshot: panel 4 green ("2 min"), panel 3
with congestion hot spots instead of a uniform blanket. Database without restarts.

## D41 — `ejecuciones_etl` table and the rest of the initial alerts (2026-11-17)
**Decision — `ejecuciones_etl` (migration 015):** one row per cycle with status (`ok` / `sin_datos` / `error`),
measured edges, requests and blocking per source, whether the predictor ran, the error and the pod. Written by
`src/ejecuciones.py` from `main.ciclo_etl` in a `finally`: the row remains even if the cycle fails, and if recording
fails the cycle carries on (it is observability, it must not bring the measurement down). Absorbed errors (a tile pass that
fails with Segment Data as fallback) are also recorded: the Job exits with 0 and `kube-state-metrics` does not see them.
**Decision — rules** (`platform/grafana/alerts/`, one file per domain, ConfigMap `grafana-alertas`):

| Rule | Source | Condition | `for` | No data |
|---|---|---|---|---|
| ETL sin correr (D40) | Prometheus | > 45 min without a successful Job | 2m | Alert |
| ETL sin datos de tiles | `ejecuciones_etl` | ≥ 3 cycles in 50 min and none measured edges | 1m | OK |
| TomTom respondió 403 | `consumo_api` | `bloqueado` today (SLP time), per API | 0s | OK |
| Consumo de TomTom por encima del ritmo | `consumo_api` | month-to-date > quota × day / days in month, per API | 10m | OK |
| API de rutas no disponible | Prometheus | available replicas < 1 (passing `/health/ready`, which checks the database) | 2m | Alert |
| API /rutas lenta | Prometheus | `/api/v1/rutas` p95 over 10 min > 2 s | 5m | OK |
| BD por encima del 80 % de su disco | SQL + Prometheus | `pg_database_size` / requested PVC size > 80 % | 10m | NoData |

**Why this way:**
- **Quota pace = 100 % of the uniform pace**, not an arbitrary percentage: `presupuesto.py` plans with
  90 % spread over the remaining days, so the ETL alone never exceeds it. Going over means another consumer
  of the same key (the Compose ETL switched on, a script) or a budget bug. The quotas are hard-coded in the rule's
  SQL; keep them aligned with `TOMTOM_CUOTA_MENSUAL` / `TOMTOM_TILES_CUOTA_MENSUAL`.
- **Disk with SQL ÷ PVC and not with `kubelet_volume_stats`:** on k3d local-path volumes are host
  directories and kubelet reports the whole disk (~1 TB, 10 % used), not the PVC. The database size against the requested size
  works the same locally and on Azure Disk (where the size is real). It does not count the WAL; the remaining 20 % covers it.
- **p95 with `and on() … > 0`:** without traffic the quantile is NaN; this way it is "no data" (OK) instead of an
  undefined value.
- **"ETL sin datos de tiles" requires ≥ 3 cycles (`HAVING`):** when the computer starts there are 0–2 recent rows; without
  that minimum it would fire together with "ETL sin correr" for the same cause.
- **No `postgres_exporter` for now:** the database alerts the plan asks for are covered with SQL; the exporter
  (connections, locks, compression) is left for when it needs to be graphed.
**Along the way:** `tests/test_asociador_db.py` and `tests/test_rol_grafana_db.py` used fixed timestamps (9 and 13
November). Once they were 7 days old, the compression policy (008) started compressing those chunks in the temporary database
while the test was inserting → *deadlock*. They now use a recent time relative to `now()`.
**Verified (k3d, 2026-11-17):**
- **Tests:** `test_ejecuciones.py` (8) and `test_ejecuciones_db.py` (4); ETL suite 180/180.
- **Real cycle (16:45 UTC):** row `ok`, 15 031 edges, 49 tile requests, 7 Segment Data, predictor, 9.8 s,
  `pod = geotracker-etl-29915565-qks4m`. The "ETL: últimos ciclos" panel shows it.
- **Rules:** all 7 loaded (`provenance: file`), health `ok`, state Normal; the `consumo_api` ones create one
  instance per API. Values at verification: Segment Data pace 28 %, tiles 12 %; database 10.6 % of the PVC; `/rutas` p95
  0.48 s (8 real requests; the `handler="/api/v1/rutas"` label exists).
- **Firing an SQL rule:** temporary copy of "Consumo por encima del ritmo" with threshold 0 → two
  Alerting alerts ("tomtom_flow_segment va al 28 % del ritmo mensual", "…vector_tiles va al 12 %…"); deleted afterwards.
- **ConfigMap replacement:** `grafana-alertas-etl` → `grafana-alertas`. When the old one was deleted, the sidecar removed
  `etl.yaml` and rewrote it from the new one (same file name): the "ETL sin correr" rule stayed loaded.

## D42 — Push notifications to the phone with ntfy (2026-11-17)
**Decision:** Grafana contact point `ntfy-celular` (webhook) to `https://ntfy.sh/<topic>`, plus the notification
policy (all alerts to that point) and a message template, provisioned from git in
`platform/grafana/alerts/notificaciones.yaml` (same `grafana-alertas` ConfigMap). The topic goes in `grafana.env`
(`NTFY_URL`, outside git) and reaches the pod via `envValueFrom`; Grafana interpolates it into the file (`${NTFY_URL}`).
- **Format:** the webhook sends Grafana's JSON; ntfy, with `?tpl=1&t={{.title}}&m={{.message}}`, takes title and
  body from that JSON. Title: `🔴 <rule>` or `✅ Resuelta: <rule>` (uses `rulename` when Grafana renames the
  alert to `DatasourceNoData`/`DatasourceError`); body: `summary` + `description`.
- **Grouping:** by folder and rule; `group_wait` 30 s, `repeat_interval` 4 h (an alert that keeps firing
  is repeated every 4 h, not on every evaluation).
- **No silence on start-up:** the daily "ETL sin correr" → "Resuelta" pair works as a notice that the cluster is back.
- **"BD por encima del 80 %": no data switches from NoData to OK.** With the contact point active a false
  `DatasourceNoData` appeared: the computer was suspended (gaps in every Prometheus series, 02:08–02:26 and 03:53–04:27
  UTC) and both evaluations of that rule (every 10 min) fell into the gap. The disk grows slowly and a missing
  database is already flagged by "API no disponible" and "ETL sin correr".
**Why ntfy:** free, no account, Android/iOS app, and it accepts a simple POST. Alternatives: Telegram (bot +
token), email (requires SMTP), Pushover (paid).
**Risks:**
- **The topic is the credential** on public `ntfy.sh`: whoever knows it can read and publish to it. The current topic
  (`geotracker`) is short and guessable; the content is rule names and values, no secrets. To change it:
  new topic in the app → `NTFY_URL` in `grafana.env` → `apply -k` → `kubectl rollout restart deployment/kps-grafana
  -n monitoring`.
- **If the computer is off or hung there is no notice:** Grafana runs on the same computer. Covering that requires an
  external watcher (*dead man's switch*, e.g. healthchecks.io); left for the cloud phase.
**Verified (k3d, 2026-11-17):** contact, policy and template loaded (`provenance: file`, URL interpolated);
temporary always-firing rule → push `🔴 PRUEBA notificación al celular` ~2.5 min later; with an
unreachable threshold → `✅ Resuelta: …`. **Deleting the ConfigMap does not delete a provisioned rule:** it stays in Grafana's
database; it is removed with a `deleteRules` file (uid) and then that ConfigMap is deleted.

## D43 — Phase 4: Compose history into the cluster by merging with `osm_id` translation (2026-11-20)
**Decision:** the history prior to the cut-over (D29, `2026-11-13 17:15 UTC`) is copied from the Compose database to the
cluster's with `deploy/k8s/scripts/migrar_historico_compose.sh` (+ `fusionar_historico.sql`): `pg_dump -Fc` backup
of the cluster → `COPY` from Compose to files → `UNLOGGED` staging tables in a `fase4` schema → **one transaction**
that translates `osm_id` and inserts → refresh of `resumen_trafico_ciclo` → compression of chunks > 7 days → per-day
verification against Compose. Compose is only read. Re-runnable: if the cluster already has data before the cut-over it skips the
merge, and if `fase4` was completed by a previous run it resumes there.
**Why not a full `pg_dump`/`pg_restore` (what the plan said):**
- The cluster already had 7 days of its own data; a full restore clashed with them or replaced them.
- **The same network has different `osm_id`s.** The 67 464 edges match by `(u,v,k)`, but 24 574 keep in Compose
  their "adopted" `osm_id` (migration 004); the cluster, loaded from scratch (D30), uses `blake2b(u,v,k)` for all of them. Copying
  the history as-is would have failed on the FK or, worse, hung readings off the wrong edges. It is translated
  `osm_id(Compose) → (u,v,k) → osm_id(cluster)`; the transaction aborts if any edge or reading is left without a match.
- `consumo_api` had already been copied at the cut-over (D29); it is not touched. `trafico_inferido` (legacy, P10) does not exist in the
  cluster's schema and is not copied.
- Facts without `ON CONFLICT`: the range is prior to the cut-over and empty in the cluster; a clash would be a broken assumption and
  must abort. `dim_segmentos_viales`, with `ON CONFLICT DO NOTHING`: the 275 Compose segments that did not
  survive deduplication (440 → 207) come in; they only name old readings (the predictor uses those with
  recent readings).
**Problem found:** with 690 MB over stdin, `kubectl exec -i` hung *after* the `COPY` had already
finished on the server. The script now sends the compressed stream (`gzip | kubectl exec … gunzip | psql`).
**Verified (k3d, 2026-11-20):**
- Prior backup: `~/geotracker-respaldos/k8s-antes-fase4-20261120T011225Z.dump` (301 MB, readable with `pg_restore -l`).
- Copied: `trafico_calles` 11 573 892 (since 2026-06-17), `fact_flujo_aristas` 2 460 732 (since 2026-11-09),
  `fact_lecturas_trafico` 18 868 (since 2026-06-10). Merge ≈ 2 min; the `CronJob` ran at the same time without issue.
- Per day and table (27 combinations), counts and exact `numeric` sums **identical** to Compose, including a sum
  weighted by `u` that would detect a mistranslated edge. The 482 Compose segments are in the cluster.
- Test route (the phase's criterion): free-flow Dijkstra between the same nodes gives the same sequence of 82
  nodes and 426.979 s in both databases (= `duracion_flujo_libre_s` of the Compose API); the free cost of the whole network
  matches. Through the API they differ only by traffic: Compose has no current traffic, the cluster does.
- Second run: skips the merge and the verification passes again. Cluster database: 2.3 → 3.0 GB.
**Pending:** retire the Compose database (the Compose API is still running against it). That resolves P10.
→ Done in D44.
Observed difference, not caused by this phase: pgRouting 4.0.1 on Compose and 4.0.2 in the cluster.

## D44 — Compose retired: database and API stopped, everything behind profiles (2026-11-20)
**Decision:** `trafico_db` and `geotracker_api` are stopped (`docker compose stop`; containers and the
`geotracker_datos_postgres` volume are kept) and move to profiles, like the ETL (D29) and Grafana (D35):
`timescaledb` in `compose`, `etl-compose` and `grafana-compose` (they all depend on it); `api` in `compose`. Without a
profile, `docker compose up -d` starts nothing. The only current environment is the cluster.
**Why:**
- After D43 the Compose database has nothing the cluster does not have (except `trafico_inferido`, legacy) and has not received data
  since 2026-11-13; its API answered routes without current traffic, which is easy to confuse with the cluster's.
- `restart: unless-stopped` already leaves it stopped after a Docker restart, but a `docker compose up -d` (a command that
  used to be in `CLAUDE.md`) brought it back up; the profile prevents that.
- The Compose API and the cluster API's `port-forward` use the same `localhost:8000`.
**Kept:** the volume (rollback: `docker compose --profile compose up -d`) and the D43 backup. Deleting the
volume is left for when the cluster has automatic backups (phase 5); today it is the second copy of the history
prior to the cut-over.
**Consequences:**
- P10 resolved: `trafico_inferido` only lives in that volume.
- Integration tests (`tests/test_asociador_db.py`): bring up only the database with
  `docker compose --profile compose up -d --wait timescaledb`; they are not pointed at the cluster's database because they create and drop
  databases.
**Verified (2026-11-20):** both containers exited with code 0; `docker compose config --services` without a profile
lists nothing, `compose` → `timescaledb api`, `etl-compose` → `timescaledb etl_worker`, `grafana-compose` →
`timescaledb grafana`; `docker compose up -d --dry-run` without a profile → "no service selected". The cluster stayed
`Running`.

## D10 — Reproducible ETL image (2026-11-09)
**Decision:** single-stage `Dockerfile` on `python:3.11.15-slim-trixie` with all dependencies at an
exact version (the ones the image in use had).
**Why:** the `Dockerfile` had two `FROM`s (the second, the database image) and could not be built; the
image in use came from an earlier version. It had to be rebuilt to add `mapbox-vector-tile`.
The previous image is kept locally as `geotracker_etl_worker:anterior`.

## D49 — API access in the cloud: token + TLS, rate limit and no public docs (2026-11-27)
(D45–D48 are reserved by the phase 2 stage A plan.)
**Decision:** in the cloud the API is reachable from any network with the `X-API-Key` token (D26) over TLS, plus:
- a **rate limit per client** with a Traefik `rateLimit` middleware on the API Ingress (429 over the limit), with
  `externalTrafficPolicy: Local` on Traefik's Service so the limit applies to the real client IP;
- an Ingress that publishes **only `/api`**: `/docs` and `/openapi.json` are reached with `kubectl port-forward`.
  Locally D32 stays as it is.

Replaces the optional IP restriction of D23.
**Why:** the phone on mobile data changes IP and the cloud environment lives for hours or days; an IP list would add
friction for limited extra protection. Neither the API code nor the Ingress manifest limits request rate today, and
the public schema is of no use to the clients.
**Discarded:** token + TLS + IP allow-list (list upkeep); private network such as Tailscale (a client on every
device, an outside service and no practice with public TLS; still possible later).
**Status:** design only; to be implemented in `overlays/nube` (phase 2, stage C). Rate limit values still open.
**Where:** `docs/superpowers/specs/2026-11-20-fase2-azure-aks-design.md` §5.3 and §6.
