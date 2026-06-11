# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ETL pipeline that collects real-time road traffic data from the TomTom Flow API for San Luis Potosí, México, and stores it in a TimescaleDB time-series database. Runs as a Docker daemon polling every 15 minutes.

## Commands

### Docker (primary workflow)
```bash
# Start TimescaleDB + ETL worker
docker-compose up --build

# Start only the database (for local development/testing)
docker-compose up timescaledb

# View ETL logs
docker logs -f etl_trafico
```

### Running tests
Tests import modules by bare name, so `PYTHONPATH` must point to `src/`:
```bash
# From project root
PYTHONPATH=src pytest tests/

# Single test file
PYTHONPATH=src pytest tests/test_transformer.py

# Single test
PYTHONPATH=src pytest tests/test_transformer.py::TestTransform::test_registro_valido_produce_dos_paquetes
```

### Topology setup (one-time)
Run before the first ETL cycle to generate `data/vialidades_maestras.json`:
```bash
cd scripts && python generador_topologia.py
```

## Architecture

### Two-phase system

**Phase 1 — Topology generation** (`scripts/`): One-time setup that queries the Overpass API (OpenStreetMap) for primary/secondary roads in SLP's bounding box, projects them to UTM (EPSG:32614), samples a monitoring point every 1 km, and writes the result to `data/vialidades_maestras.json`. This file is the static input for all subsequent ETL runs.

**Phase 2 — ETL daemon** (`src/`): A `while True` loop in `main.py` orchestrates three modules in sequence every 15 minutes:

| Module | Responsibility |
|---|---|
| `extractor.py` | Async concurrent HTTP to TomTom API with exponential backoff + jitter; semaphore-limited to 20 simultaneous requests |
| `transformer.py` | Splits raw JSON into two output streams; computes congestion ratio (`currentTravelTime / freeFlowTravelTime`); handles missing keys with `None` |
| `loader.py` | Bulk upsert into two tables using SQLAlchemy Core + `ON CONFLICT DO NOTHING` |

### Database schema

`dim_segmentos_viales` — static street dimension; geometry stored as JSONB; keyed by `id_segmento`.

`fact_lecturas_trafico` — TimescaleDB hypertable partitioned on `timestamp_lectura`; unique constraint `(timestamp_lectura, id_segmento)` enforces idempotency.

`loader.create_tables()` creates both tables and calls `create_hypertable()` idempotently on startup.

### Configuration (`src/config.py`)

Reads all secrets from environment variables (`.env` → `docker-compose.yml` → container). `COORDENADAS_ESTATICAS` is loaded from `data/vialidades_maestras.json` at import time. `HORAS_PICO` defines analytical hours for optional scheduling.

## Key constraints (from ARCHITECTURE.md)

- **No `print()`** — use the `logging` module with INFO/WARNING/ERROR levels throughout.
- **No iterative inserts** — all DB writes go through SQLAlchemy bulk insert.
- **All network I/O must be async** — use `aiohttp`/`asyncio`; no `requests` in the ETL path.
- **Coordinate geometry never goes in `fact_lecturas_trafico`** — coordinates stay in the JSONB column of `dim_segmentos_viales`.
- **Pin all dependencies** with exact versions in `requirements.txt`.
