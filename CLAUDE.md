# CLAUDE.md

Guía para Claude Code (y cualquier desarrollador) al trabajar en este repositorio.

## Resumen del proyecto

**geoTracker** mide el tráfico vial de San Luis Potosí, México, con la TomTom Flow API, lo guarda en
TimescaleDB, infiere la congestión en toda la red vial de OpenStreetMap (IDW) para mostrar un mapa de
calor en Grafana y — como siguiente fase — calculará la **ruta más rápida real** entre dos puntos con
Dijkstra (pgRouting) ponderado por el tráfico actual.

Documentación:
- `docs/ARCHITECTURE.md` — reglas obligatorias (léelas antes de cambiar código).
- `docs/DATABASE.md` — esquema real de la BD y deuda técnica numerada (P1–P11).
- `docs/ROUTING.md` — diseño del ruteo con tráfico.
- `docs/DECISIONES.md` — registro de decisiones (qué y por qué). **Agrega una entrada por cada decisión
  de diseño** y cítala en el commit.

## Comandos

### Docker
```bash
docker compose up --build              # BD + ETL + Grafana
docker compose up -d timescaledb       # solo la BD (desarrollo / tests)
docker logs -f etl_trafico             # logs del ETL
docker exec -it trafico_db psql -U user -d vialidades_slp   # consola SQL
```
Grafana: http://localhost:3000

### Tests
Los tests importan módulos por nombre simple; `PYTHONPATH` debe apuntar a `src/`:
```bash
PYTHONPATH=src pytest tests/
PYTHONPATH=src pytest tests/test_transformer.py::TestTransform::test_registro_valido_produce_dos_paquetes
```

### Esquema de base de datos (migraciones)
Todo el DDL vive en `db/migrations/NNN_nombre.sql`. `main.py` aplica las pendientes al arrancar;
también se pueden aplicar a mano. Lo aplicado se registra en `schema_migrations`.
```bash
cd src && python migrate.py
```
Para cambiar el esquema: crea el siguiente `NNN_*.sql` (idempotente, con `IF NOT EXISTS`), nunca edites
uno ya aplicado, y actualiza `docs/DATABASE.md`.

### Preparación (una sola vez, o al actualizar la red)
```bash
# 1. Puntos de monitoreo TomTom → data/vialidades_maestras.json
cd scripts && python generador_topologia.py

# 2. Red vial OSM + topología de ruteo → dim_calles_osm y dim_calles_osm_vertices_pgr
#    Requiere migraciones aplicadas. Ejecutar DESDE LA RAÍZ para reutilizar la caché ./cache de osmnx.
python scripts/osmnx_loader.py
```
`osmnx_loader.py` es idempotente: se puede re-ejecutar sin duplicar la red ni romper el histórico.

Después de regenerar los puntos (paso 1) y de medirlos al menos una vez, quita los que devuelven el mismo
segmento TomTom: `python scripts/deduplicar_puntos.py`. `scripts/prueba_vector_tiles.py` es una prueba
de la fuente alternativa (vector tiles); requiere `pip install mapbox-vector-tile==2.1.0`.

Variables opcionales si cambia el plan de TomTom: `TOMTOM_CUOTA_MENSUAL` (Segment Data, default 20000) y
`TOMTOM_TILES_CUOTA_MENSUAL` (vector tiles, default 200000).

Los tests de `tests/test_asociador_db.py` crean y borran una BD temporal en el PostgreSQL de `DB_*`; se
saltan si no hay BD alcanzable.

### Entorno Python sin instalar nada en el host
La imagen `geotracker_etl_worker` trae todas las dependencias (incluye osmnx/geopandas):
```bash
docker run --rm --entrypoint "" -v "$PWD":/app -w /app --network geotracker_red_trafico \
  --env-file .env geotracker_etl_worker bash -c "PYTHONPATH=src python -m pytest tests"
```

## Arquitectura

### Fases

| Fase | Código | Frecuencia | Salida |
|---|---|---|---|
| 1. Puntos de monitoreo | `scripts/generador_topologia.py` (+ `cliente_overpass.py`, `procesador_espacial.py`) | Una vez | `data/vialidades_maestras.json` (440 puntos; vías trunk/primary/secondary/tertiary muestreadas cada 1 km en UTM 14N, solo dentro de la zona) |
| 0. Esquema | `src/migrate.py` + `db/migrations/` | Al arrancar | Tablas, índices, hypertables |
| 2. Red vial OSM + topología | `scripts/osmnx_loader.py` (+ `osm_utils.py`) | Cada varios meses | `dim_calles_osm` (67 464 aristas; `source/target` = nodos OSM), `dim_calles_osm_vertices_pgr` (28 008 nodos) |
| 4. ETL + inferencia | `src/main.py` | Cada 15 min | `fact_lecturas_trafico`, `trafico_calles` |
| 5. Ruteo | `src/router.py` / `services/api` | Bajo demanda | **Pendiente** — ver `docs/ROUTING.md` |

### Ciclo del daemon (`src/main.py`)

| Módulo | Responsabilidad |
|---|---|
| `config.py` | Variables de entorno, carga de `vialidades_maestras.json`, constantes (`HORAS_PICO`, límites TomTom) |
| `zona.py` | **Zona de estudio única** (bbox de la zona metropolitana de SLP + Soledad). La usan `generador_topologia.py` y `osmnx_loader.py`; nunca definas otro bbox |
| `extractor_tiles.py` | **Fuente principal** (D8): pasada asíncrona de 49 Traffic Flow vector tiles (zoom 14) → tramos con `traffic_level` y `road_closure` |
| `asociador.py` | Asocia tramos de tiles a aristas OSM **dirigidas** (D9) en PostGIS → `fact_flujo_aristas` |
| `extractor.py` | Fuente secundaria: Flow Segment Data. HTTP asíncrono; semáforo de 20; backoff exponencial + jitter; timeout 10 s |
| `migrate.py` | Aplica migraciones pendientes al arrancar |
| `presupuesto.py` | Pool de peticiones a TomTom: cupo por ciclo según cuota mensual, hora (pico/valle/madrugada) y consumo en `consumo_api`; elige los puntos con lectura más antigua |
| `transformer.py` | Separa cada respuesta en paquete estático `(id, nombre, coordenadas, wkt)` y dinámico (velocidad, tiempo, congestión) |
| `loader.py` | Upsert masivo: segmentos (`geom` vía `ST_GeomFromText`, solo completa si era NULL) y lecturas (`ON CONFLICT DO NOTHING`) |
| `predictor.py` | IDW: última lectura por segmento → puntos cada 100 m → K=5 vecinos (p=2, radio 600 m) sobre centroides OSM activos → `trafico_calles`. Idempotente: `timestamp_calculo` = lectura más reciente; omite ciclos ya inferidos |

### Base de datos (resumen — detalle en `docs/DATABASE.md`)

| Tabla | Tipo | Contenido |
|---|---|---|
| `dim_segmentos_viales` | dimensión | Segmentos TomTom; `geom` LineString 4326 (+ JSONB legado) |
| `fact_lecturas_trafico` | hypertable | Lecturas TomTom cada 15 min |
| `dim_calles_osm` | dimensión / aristas del grafo | Red OSM; clave natural `(u,v,k)`, `geom` 4326, `centroide_m` 32614, `id/source/target`, `maxspeed_kmh`, `oneway`, `activo` |
| `dim_calles_osm_vertices_pgr` | nodos del grafo | Intersecciones (`id` = nodo OSM, PK + GiST) |
| `schema_migrations` | control | Migraciones aplicadas |
| `trafico_calles` | hypertable | Congestión inferida por `osm_id` y ciclo |
| `trafico_inferido` | **legado** | IDW v1 sobre grilla; ya no se escribe |

Extensiones: PostGIS 3.3, pgRouting 4.0, TimescaleDB 2.10.

### Configuración
Secretos por variables de entorno (`.env` → `docker-compose.yml` → contenedor). Ojo: `config.py` lee
`DB_PASSWORD`; el compose define `DB_PASS` (hoy funciona solo porque se monta `.:/app` y
`load_dotenv()` lee `.env`).

## Reglas clave (de `ARCHITECTURE.md`)

- Sin `print()` — usa `logging`.
- Sin inserts fila a fila; inserciones por lotes e idempotentes (`ON CONFLICT`).
- E/S de red del ETL asíncrona (`aiohttp`); `requests` solo en `scripts/`.
- Geometría en PostGIS con SRID explícito (4326 / 32614); nunca en tablas de hechos. JSONB es legado.
- Grafo dirigido: `pgr_dijkstra(..., directed => true)` con `reverse_cost = -1`; costo en segundos; solo aristas `activo`.
- Con listas de osmnx (`highway`, `name`…) nunca tomes "el primero": su orden cambia con `PYTHONHASHSEED`. Usa `scripts/osm_utils.py`.
- Todo cambio de esquema va en `db/migrations/` y se refleja en `docs/DATABASE.md`.
- Dependencias con versión exacta.

## Estado actual y prioridades

1. Reestructuración a vector tiles (D8) en curso: ingesta lista (`fact_flujo_aristas`); falta el predictor v3 que use las aristas medidas. Cuotas: tiles 200 000/mes, Segment Data 20 000/mes, ambas con pool en `consumo_api`.
2. Saneamiento de esquema: P1–P9 resueltos (2026-11-09). Pendientes: P10–P11 (ver `docs/DATABASE.md`).
3. Infra: arreglar `Dockerfile` del ETL (doble `FROM`), credenciales del compose a `.env`.
4. Ruteo: vista `v_costos_grafo`, `router.py`, endpoint en `services/api` (hoy `app/api` y `app/core` están vacíos).
