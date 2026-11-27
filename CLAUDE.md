# CLAUDE.md

Guide for Claude Code (and any developer) working in this repository.

## Project summary

**geoTracker** measures road traffic in San Luis Potosí, Mexico, with the TomTom Flow API, stores it in
TimescaleDB, infers congestion over the whole OpenStreetMap road network (IDW) to show a heatmap
in Grafana and — as the next phase — will compute the **actual fastest route** between two points with
Dijkstra (pgRouting) weighted by current traffic.

Documentation:
- `docs/ARCHITECTURE.md` — mandatory rules (read them before changing code).
- `docs/DATABASE.md` — actual database schema and numbered technical debt (P1–P11).
- `docs/ROUTING.md` — design of traffic-aware routing.
- `docs/DECISIONES.md` — decision log (what and why). **Add an entry for every design
  decision** and cite it in the commit.
- `docs/KUBERNETES.md` — analysis and plan of the Kubernetes milestone (not implemented yet).

## Commands

### Kubernetes (current environment — k3d, namespace `geotracker`)
```bash
kubectl get pods,cronjob -n geotracker                                   # status
kubectl port-forward -n geotracker svc/geotracker-api 8000:80            # API at http://localhost:8000/docs
kubectl exec -it -n geotracker geotracker-db-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'   # SQL console
kubectl logs -n geotracker job/<geotracker-etl-NNN>                      # logs of one ETL cycle
```
Building/importing images and applying manifests: `docs/KUBERNETES.md`.

### Docker Compose (retired — D44)
All of `docker-compose.yml` is behind profiles: `docker compose up -d` without a profile starts nothing. The Compose
database was frozen at the cut-over (2026-11-13 17:02 UTC); its history is already in the cluster (D43). Its
`geotracker_datos_postgres` volume is kept.

| Service | Container | Profile |
|---|---|---|
| Database | `trafico_db` (`localhost:5432`) | `compose`, `etl-compose`, `grafana-compose` |
| API | `geotracker_api` (`localhost:8000`, clashes with the port-forward) | `compose` |
| ETL | `etl_trafico` | `etl-compose` (never together with the `CronJob`, D29) |
| Grafana | `grafana_bi` | `grafana-compose`; the current one is the cluster's, on :3001 (D33, D35) |

```bash
docker compose --profile compose up -d --wait timescaledb   # database only (e.g. integration tests)
docker compose --profile compose stop                       # shut it down again
```

On Kubernetes, Grafana + Prometheus come from the `kube-prometheus-stack` chart (namespace `monitoring`):
`kubectl port-forward -n monitoring svc/kps-grafana 3001:80`. Installation and operations: `docs/KUBERNETES.md`
("Observability: operations"). Dashboards as code in `deploy/k8s/platform/grafana/dashboards/`.

⚠️ The database image is pinned (PostgreSQL 15.2 / TimescaleDB 2.10.2 / pgRouting 4.0.1). Do **not** use
`docker compose build timescaledb` or change `db/Dockerfile` without planning the extension upgrade (D17).

### API
```bash
kubectl port-forward -n geotracker svc/geotracker-api 8000:80 &
curl -H "X-API-Key: $TOKEN" 'localhost:8000/api/v1/rutas?origen=22.1497,-100.9739&destino=22.1460,-101.0130'   # &detalle=true
```
Response: distance, duration with traffic and at free flow, delay, traffic freshness, streets in order and
GeoJSON geometry. 422 if the coordinate is > 300 m from the network or the format is invalid; 404 if there is no route.
`/api/v1/*` requires `X-API-Key` with a token from `API_TOKENS` (in the cluster, `secret.env`; 401 if missing). Without tokens the API does not start (D26).
No token needed: `/health/live` (liveness, does not touch the database), `/health/ready` (readiness, 503 if the database fails), `/health`
(informational, always 200) and `/metrics` (Prometheus). The Ingress only publishes `/api`, `/docs` and `/openapi.json` (D32).
On Kubernetes the secrets go in `deploy/k8s/overlays/local/secret.env` (outside git, D27).

### Tests
Tests import modules by plain name; `PYTHONPATH` must point to `src/`:
```bash
PYTHONPATH=src pytest tests/
PYTHONPATH=src pytest tests/test_transformer.py::TestTransform::test_registro_valido_produce_dos_paquetes
PYTHONPATH=src:services/api pytest services/api/tests     # API (in the geotracker-api image)
```

### Database schema (migrations)
All DDL lives in `db/migrations/NNN_nombre.sql`. `main.py` applies pending ones on startup;
they can also be applied by hand. What has been applied is recorded in `schema_migrations`.
```bash
cd src && python migrate.py
```
To change the schema: create the next `NNN_*.sql` (idempotent, with `IF NOT EXISTS`), never edit
one that has already been applied, and update `docs/DATABASE.md`.

### Preparation (once, or when updating the network)
```bash
# 1. TomTom monitoring points → data/vialidades_maestras.json
cd scripts && python generador_topologia.py

# 2. OSM road network + routing topology → dim_calles_osm and dim_calles_osm_vertices_pgr
#    Requires migrations applied. Run FROM THE ROOT (downloads to ./cache/osm).
#    Default source: the state's .osm.pbf extract (OSM_PBF_URL) — the public Overpass rejects automated
#    clients (D30). --pbf file.pbf uses a local one; --fuente overpass, the API or its ./cache.
python scripts/osmnx_loader.py
```
`osmnx_loader.py` is idempotent: it can be re-run without duplicating the network or breaking the history.

After regenerating the points (step 1) and measuring them at least once, remove those that return the same
TomTom segment: `python scripts/deduplicar_puntos.py`. `scripts/prueba_vector_tiles.py` is a test
of the alternative source (vector tiles); it requires `pip install mapbox-vector-tile==2.1.0`.

Optional variables if the TomTom plan changes: `TOMTOM_CUOTA_MENSUAL` (Segment Data, default 20000) and
`TOMTOM_TILES_CUOTA_MENSUAL` (vector tiles, default 200000).

The tests in `tests/test_asociador_db.py` create and drop a temporary database on the PostgreSQL in `DB_*`; they
are skipped if no database is reachable. With Compose stopped (D44), first bring up only its database:
`docker compose --profile compose up -d --wait timescaledb` (do not use the cluster's database for this).

### Python environment without installing anything on the host
The `geotracker_etl_worker` image ships all dependencies (including osmnx/geopandas):
```bash
docker run --rm --entrypoint "" -v "$PWD":/app -w /app --network geotracker_red_trafico \
  --env-file .env geotracker_etl_worker bash -c "PYTHONPATH=src python -m pytest tests"
```

## Architecture

### Phases

| Phase | Code | Frequency | Output |
|---|---|---|---|
| 1. Monitoring points | `scripts/generador_topologia.py` (+ `cliente_overpass.py`, `procesador_espacial.py`) | Once | `data/vialidades_maestras.json` (440 points; trunk/primary/secondary/tertiary roads sampled every 1 km in UTM 14N, only inside the area) |
| 0. Schema | `src/migrate.py` + `db/migrations/` | On startup | Tables, indexes, hypertables |
| 2. OSM road network + topology | `scripts/osmnx_loader.py` (+ `osm_utils.py`, `fuente_pbf.py`) | Every few months | `dim_calles_osm` (67 464 edges; `source/target` = OSM nodes), `dim_calles_osm_vertices_pgr` (28 008 nodes) |
| 4. ETL + inference | `src/main.py` | Every 15 min | `fact_lecturas_trafico`, `trafico_calles` |
| 5. Routing | `ruta_mas_rapida()` in the database (011–012) + `GET /api/v1/rutas` (`services/api`) | On demand | Fastest route with current traffic — see `docs/ROUTING.md` |

### Daemon cycle (`src/main.py`)

| Module | Responsibility |
|---|---|
| `config.py` | Environment variables, loading `vialidades_maestras.json`, constants (`HORAS_PICO`, TomTom limits) |
| `zona.py` | **Single study area** (bbox of the SLP metropolitan area + Soledad). Used by `generador_topologia.py` and `osmnx_loader.py`; never define another bbox |
| `extractor_tiles.py` | **Main source** (D8): async pass over 49 Traffic Flow vector tiles (zoom 14) → segments with `traffic_level` and `road_closure` |
| `asociador.py` | Matches tile segments to **directed** OSM edges (D9) in PostGIS → `fact_flujo_aristas` |
| `extractor.py` | Secondary source: Flow Segment Data. Async HTTP; semaphore of 20; exponential backoff + jitter; 10 s timeout |
| `migrate.py` | Applies pending migrations on startup |
| `presupuesto.py` | TomTom request pool: per-cycle allowance based on the monthly quota, hour (peak/off-peak/night) and usage in `consumo_api`; picks the points with the oldest reading |
| `transformer.py` | Splits each response into a static package `(id, nombre, coordenadas, wkt)` and a dynamic one (speed, time, congestion) |
| `loader.py` | Bulk upsert: segments (`geom` via `ST_GeomFromText`, only filled in if it was NULL) and readings (`ON CONFLICT DO NOTHING`) |
| `predictor.py` | **v3** (D13): latest tile pass → measured edges with their value (`TILE`) + IDW (K=5, p=2, 600 m) from the open measurements for the rest (`IDW_TILE`). **v2** fallback with Segment Data (`IDW_OSM`). → `trafico_calles`. Idempotent: `timestamp_calculo` = timestamp of the source data |

### Database (summary — detail in `docs/DATABASE.md`)

| Table | Type | Content |
|---|---|---|
| `dim_segmentos_viales` | dimension | TomTom segments; `geom` LineString 4326 (+ legacy JSONB) |
| `fact_lecturas_trafico` | hypertable | TomTom readings every 15 min |
| `dim_calles_osm` | dimension / graph edges | OSM network; natural key `(u,v,k)`, `geom` 4326, `centroide_m` 32614, `id/source/target`, `maxspeed_kmh`, `oneway`, `activo` |
| `dim_calles_osm_vertices_pgr` | graph nodes | Intersections (`id` = OSM node, PK + GiST) |
| `schema_migrations` | control | Applied migrations |
| `trafico_calles` | hypertable | Inferred congestion per `osm_id` and cycle |
| `trafico_inferido` | **legacy** | IDW v1 over a grid; no longer written |

Extensions: PostGIS 3.3, pgRouting 4.0, TimescaleDB 2.10.

### Configuration
Secrets via environment variables (`.env` → `docker-compose.yml` → container). Watch out: `config.py` reads
`DB_PASSWORD`; the compose file defines `DB_PASS` (today it only works because `.:/app` is mounted and
`load_dotenv()` reads `.env`).

## Key rules (from `ARCHITECTURE.md`)

- No `print()` — use `logging`.
- No row-by-row inserts; batched, idempotent inserts (`ON CONFLICT`).
- ETL network I/O is async (`aiohttp`); `requests` only in `scripts/`.
- Geometry in PostGIS with an explicit SRID (4326 / 32614); never in fact tables. JSONB is legacy.
- Directed graph: `pgr_dijkstra(..., directed => true)` with `reverse_cost = -1`; cost in seconds; only `activo` edges. Always use `v_costos_grafo` / `ruta_mas_rapida()`: they already exclude closed roads and self-loops.
- With osmnx lists (`highway`, `name`…) never take "the first one": their order changes with `PYTHONHASHSEED`. Use `scripts/osm_utils.py`.
- Every schema change goes in `db/migrations/` and is reflected in `docs/DATABASE.md`.
- Dependencies with exact versions.

## Current status and priorities

1. **ETL running since 2026-11-09 21:21 UTC** collecting data; since 2026-11-13 17:15 UTC it runs as a `CronJob`
   on k3d (D29) and the Compose one is stopped; the earlier history (since 2026-06) is already in the cluster (phase 4, D43:
   `deploy/k8s/scripts/migrar_historico_compose.sh`) (vector tiles + Segment Data, ~12 s cycle every 15 min). Pending: confirm in TomTom's terms that storing data derived from tiles is allowed. Quotas: tiles 200 000/month, Segment Data 20 000/month, both with a pool in `consumo_api`.
2. Routing: exposed at `GET /api/v1/rutas` (D16). Pending: validate ETAs against several days of data and calibrate `dim_velocidad_tipo_via`.
3. Observability (phase 3, D31): probes and `/metrics` in the API (D32); stack, Grafana 13.0.2, `grafana_ro` role and
   versioned dashboard on k3d (D33); tile-based dashboard (D36–D38, D40); alerts as code and the
   `ejecuciones_etl` table (D40, D41); phone notifications with ntfy (D42). Next: `postgres_exporter`.
4. Infra: local Kubernetes (k3d) is the current environment; phases 1, 3 (except `postgres_exporter`) and 4 done (D43) and
   Compose retired behind profiles (D44). Next: phase 2 (Azure/AKS with Terraform, CloudNativePG, TLS) and CI/CD
   (Jenkins on a dedicated VM under evaluation) — **read `docs/KUBERNETES.md`** (open decisions and phased plan).
5. Schema: P1–P11 resolved (P10: `trafico_inferido` only remains in the retired Compose volume, D44).
