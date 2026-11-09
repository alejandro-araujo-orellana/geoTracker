# Esquema de Base de Datos — geoTracker

> Documento levantado **contra la base de datos real** (`trafico_db`, BD `vialidades_slp`) el 2026-11-09
> y actualizado tras aplicar las migraciones 001–009 y ampliar la red OSM a la zona única el mismo día.
> El DDL completo vive en `db/migrations/`; si cambias el esquema, agrega una migración y actualiza
> este archivo en el mismo commit.

## Motor y extensiones

Imagen: `timescale/timescaledb-ha:pg15-latest` + paquete `postgresql-15-pgrouting` (ver `db/Dockerfile`).

| Extensión | Versión | Uso |
|---|---|---|
| `postgis` | 3.3.2 | Geometrías (`geometry`), índices GiST, reproyección |
| `pgrouting` | 4.0.1 | Grafo vial y Dijkstra (`pgr_dijkstra`, `pgr_connectedComponents`) |
| `timescaledb` | 2.10.2 | Hypertables para series de tiempo |
| `timescaledb_toolkit` | 1.16.0 | Instalada, sin uso actual |

Sistemas de referencia usados:
- **EPSG:4326** (WGS84, grados): toda geometría "de presentación" (`geom`) — lo que consume Grafana y pgRouting.
- **EPSG:32614** (UTM 14N, metros): cálculos de distancia (IDW, muestreo). Solo en `dim_calles_osm.centroide_m`.

## Diagrama lógico

```
                 ┌─────────────────────────┐
  TomTom ──ETL──▶│ dim_segmentos_viales    │◀── (FK) trafico_inferido  [LEGADO]
                 │  482 segmentos medidos  │
                 └───────────┬─────────────┘
                             │ id_segmento (sin FK)
                 ┌───────────▼─────────────┐
                 │ fact_lecturas_trafico   │  hypertable
                 │  lecturas cada 15 min   │
                 └───────────┬─────────────┘
                             │ predictor.py (IDW)
                             ▼
 OSM ──osmnx_loader──▶ ┌─────────────────────┐        ┌────────────────────────────┐
                       │ dim_calles_osm      │◀─(FK)──│ trafico_calles  hypertable │
                       │  67 464 aristas     │        │  congestión inferida/calle │
                       │  source/target ─────┼──┐     └────────────────────────────┘
                       └─────────────────────┘  │
                       ┌─────────────────────────▼──┐
                       │ dim_calles_osm_vertices_pgr│  28 008 nodos (intersecciones)
                       └────────────────────────────┘
```

## Tablas

### `dim_segmentos_viales` — dimensión de segmentos medidos por TomTom
| Columna | Tipo | Notas |
|---|---|---|
| `id_segmento` | `text` PK | Clave de `data/vialidades_maestras.json` (ej. `Avenida_Universidad_1`) |
| `nombre_via` | `text` NOT NULL | Hoy es idéntico a `id_segmento` |
| `coordenadas_geometria` | `jsonb` | Lista `[{latitude, longitude}, ...]` que devuelve TomTom. **Legado**, se mantiene como respaldo |
| `geom` | `geometry(LineString,4326)` | Geometría PostGIS. Índice GiST `idx_segmentos_geom` |

Estado: 482 filas, **todas con `geom`** (la migración 003 rellenó las 282 que faltaban desde el JSONB).
`loader.py` escribe `geom` en cada upsert: inserta los segmentos nuevos y completa la geometría de
los que la tenían en NULL. Una geometría existente no se sobrescribe.
35 de estos segmentos quedaron fuera de la zona de estudio (P9) y otros 233 eran redundantes (devolvían el
mismo segmento TomTom que otro punto; `scripts/deduplicar_puntos.py`). Hoy se miden 207; el resto se
conserva por su histórico en `fact_lecturas_trafico`.

### `fact_lecturas_trafico` — hechos de tráfico (hypertable sobre `timestamp_lectura`)
| Columna | Tipo | Notas |
|---|---|---|
| `timestamp_lectura` | `timestamptz` NOT NULL | UTC |
| `id_segmento` | `text` NOT NULL | Referencia lógica a `dim_segmentos_viales` (sin FK) |
| `velocidad_actual` | `double precision` | `currentSpeed` km/h |
| `tiempo_recorrido` | `double precision` | `currentTravelTime` s |
| `nivel_congestion` | `double precision` | `currentTravelTime / freeFlowTravelTime` (1.0 = flujo libre; observado 0–8, media 1.14) |

Restricción `uq_lectura_por_calle UNIQUE (timestamp_lectura, id_segmento)` → idempotencia.
Datos: 17 030 filas, 2026-06-10 → 2026-07-01. Chunks de 7 días. Compresión a los 7 días
(`segmentby id_segmento`, `orderby timestamp_lectura DESC`). **Sin retención**: son los datos crudos y
la base para costos históricos por hora del día.

### `dim_calles_osm` — red vial completa de OSM (aristas del grafo)
| Columna | Tipo | Notas |
|---|---|---|
| `osm_id` | `bigint` PK | ID estable. Filas nuevas: `blake2b("u_v_k")` (63 bits). Las 24 574 filas cargadas antes del 2026-11-09 conservan su ID original para no romper el histórico |
| `u`, `v`, `k` | `bigint`, `bigint`, `integer` | **Clave natural** (único `uq_calles_osm_uvk`): nodo OSM inicial, nodo OSM final, índice de arista paralela |
| `id` | `bigint` NOT NULL, serial | ID de arista para pgRouting |
| `nombre` | `text` | NULL si no tiene nombre. Si OSM tiene varios, se guarda el primero en orden alfabético |
| `tipo_via` | `text` NOT NULL | `highway` de OSM. Si la arista fusiona varios tipos, se guarda el de mayor jerarquía |
| `longitud_m` | `double precision` | Longitud en metros (osmnx) |
| `maxspeed_kmh` | `double precision` | Límite de OSM en km/h (escaso: ~1 500 aristas) |
| `oneway` | `boolean` | Sentido único según OSM |
| `activo` | `boolean` NOT NULL default TRUE | FALSE si la arista ya no existe en la última descarga de OSM. No se borra porque `trafico_calles` la referencia |
| `geom` | `geometry(LineString,4326)` | **El sentido de la línea es el sentido de circulación** |
| `centroide_m` | `geometry(Point,32614)` | Precalculado para el IDW |
| `source`, `target` | `bigint` | = `u`, `v` (IDs de nodo OSM) → `dim_calles_osm_vertices_pgr.id`. NULL si `activo = FALSE` |

Índices: GiST en `geom` y `centroide_m`; btree en `tipo_via`, `source`, `target`, `activo`; único en `(u, v, k)`.

Datos: 67 464 aristas activas (54 588 residential) en la zona única de `src/zona.py`, 28 008 vértices,
**1 único componente conexo**. Hasta el 2026-11-09 la red cubría solo el centro (24 574 aristas); al
ampliarla, esas aristas conservaron su `osm_id` y se agregaron 42 890.

Carga: `scripts/osmnx_loader.py` hace todo en una transacción: staging → adopción de filas antiguas por
geometría → upsert por `(u, v, k)` → desactivación de aristas desaparecidas → sincronización de vértices
→ validación (aristas sin vértice = 0; componentes conexos). Es idempotente; verificado con tres
`PYTHONHASHSEED` distintos.

**Direccionalidad:** proviene de un grafo dirigido de osmnx (`network_type="drive"`). Una calle de
doble sentido aparece como **dos aristas** con geometría invertida (en la red original, 17 491 de
24 574 aristas tenían par inverso);
una calle de un solo sentido aparece como **una** arista. Por tanto, para Dijkstra se debe usar
`directed => true` y `reverse_cost = -1`. Usar `reverse_cost = cost` permitiría circular en contrasentido.

### `dim_calles_osm_vertices_pgr` — nodos del grafo (intersecciones)
| Columna | Tipo |
|---|---|
| `id` | `bigint` PK — ID de nodo OSM |
| `x`, `y` | `double precision` (lon/lat) |
| `geom` | `geometry(Point,4326)`, índice GiST `idx_vertices_geom` |

La mantiene `osmnx_loader.py` a partir de los nodos de osmnx: solo contiene nodos usados por aristas
activas. El nodo más cercano a una coordenada se obtiene con `ORDER BY geom <-> punto LIMIT 1`, que
usa el índice GiST. Las columnas `in_edges`/`out_edges` de `pgr_extractVertices` se eliminaron
(migración 005): se derivan de `dim_calles_osm`.

### `consumo_api` — consumo de APIs externas (migración 009)
| Columna | Tipo | Notas |
|---|---|---|
| `fecha` | `date` | Día en hora local de SLP (PK con `api`) |
| `api` | `text` | `tomtom_flow_segment` (ETL) o `tomtom_flow_vector_tiles` (prueba de tiles) |
| `peticiones` | `integer` | Cada intento HTTP, incluidos reintentos |
| `bloqueado` | `boolean` | Se recibió un 403 ese día: no se vuelve a llamar hasta el siguiente |

La escribe y la lee `src/presupuesto.py` para no exceder la cuota mensual (ver `docs/ROUTING.md` › Fuentes de tráfico).

### `schema_migrations` — control de migraciones
`version` (PK, `'001'`…), `nombre`, `aplicada_en`. La escribe `src/migrate.py`.

### `trafico_calles` — congestión inferida por calle OSM (hypertable sobre `timestamp_calculo`)
| Columna | Tipo | Notas |
|---|---|---|
| `id` | `bigint` serial | Sin PK |
| `timestamp_calculo` | `timestamptz` NOT NULL | **Estado del tráfico inferido**: la lectura TomTom más reciente usada (`max(timestamp_lectura)`). Hasta el 2026-07-01 era la hora de cálculo |
| `osm_id` | `bigint` NOT NULL FK → `dim_calles_osm(osm_id)` | |
| `nivel_congestion_inf` | `double precision` NOT NULL | IDW, recortado a [1.0, 3.0] |
| `metodo_inferencia` | `text` default `'IDW_OSM'` | |

Restricción `uq_trafico_calles_ciclo UNIQUE (timestamp_calculo, osm_id)` (migración 006). `predictor.py`
no recalcula si ese estado del tráfico ya existe, e inserta con `ON CONFLICT ... DO NOTHING`. Re-ejecutarlo
sobre los mismos datos no duplica filas (verificado en una copia de la BD).

Índices: `trafico_calles_timestamp_calculo_idx` (btree `timestamp_calculo DESC`, el que crea TimescaleDB
por defecto) y el único `uq_trafico_calles_ciclo`.

Volumen y ciclo de vida (migración 008): con la red ampliada son ~67 k filas por ciclo (~6.5 M/día con
ciclos cada 15 min). Chunks de **1 día** (los anteriores al cambio son de 7), **compresión** a los 7 días
(`segmentby osm_id`, `orderby timestamp_calculo DESC`; en la copia de prueba 45 MB → 31 MB) y
**retención de 365 días**. Como TimescaleDB 2.10 no admite `ON CONFLICT` en chunks comprimidos,
`predictor.py` no infiere si la lectura más reciente tiene más de 7 días (`ANTIGUEDAD_MAXIMA`).
Como fuentes del IDW solo usa lecturas de las últimas 6 h (`VENTANA_LECTURAS`): con el pool cada ciclo
mide pocos puntos y las lecturas viejas no deben pasar por tráfico actual.

Datos: 442 332 filas en 18 ciclos (2026-06-17 → 2026-07-01).

### `trafico_inferido` — **LEGADO** (IDW v1 sobre grilla de puntos)
`id`, `timestamp_calculo`, `segmento_origen_id` (FK → `dim_segmentos_viales`), `nivel_congestion_inf`,
`distancia_metros`, `metodo_inferencia` (`IDW_GRID`), `lat`, `lon`.
152 834 filas, último dato 2026-06-17. No es hypertable. Ya no se escribe. Candidata a eliminarse
cuando Grafana deje de consultarla.

## Migraciones (`db/migrations/`)

`src/migrate.py` las aplica en orden, cada una en su propia transacción, y las registra en
`schema_migrations`. `main.py` lo invoca al arrancar. Todas son idempotentes: se verificaron sobre la
BD existente y sobre una BD vacía.

| Versión | Contenido |
|---|---|
| 001 | Extensiones `timescaledb`, `postgis`, `pgrouting` |
| 002 | Línea base: las 5 tablas del sistema, índices y hypertables (lo que antes era SQL manual) |
| 003 | (P2) Backfill de `dim_segmentos_viales.geom` desde el JSONB |
| 004 | (P3/P4) `u, v, k, maxspeed_kmh, oneway, activo` + único `(u, v, k)`; limpia `nombre = 'NaN'` |
| 005 | (P5) PK e índice GiST en vértices; tipa `geom` como `Point,4326`; quita `in_edges/out_edges` |
| 006 | (P6) `UNIQUE (timestamp_calculo, osm_id)` en `trafico_calles` |
| 007 | (P7) Elimina el índice duplicado `idx_trafico_calles_tiempo` |
| 008 | (P8) Compresión a 7 días en ambas hypertables; retención de 365 días en `trafico_calles`; chunks diarios en `trafico_calles` |
| 009 | Tabla `consumo_api` (pool de peticiones a TomTom) |

`trafico_inferido` (legado) no está en las migraciones: existe solo en la BD actual.

## Problemas conocidos (deuda técnica del esquema)

| # | Problema | Impacto | Acción propuesta |
|---|---|---|---|
| P1 | ✅ **Resuelto** — DDL fuera del repo | — | Migraciones 001–005 + `src/migrate.py` |
| P2 | ✅ **Resuelto** — `loader.py` no escribía `geom` | — | Migración 003 + `transformer.coordenadas_a_wkt` + upsert con `ST_GeomFromText` |
| P3 | ✅ **Resuelto** — `osm_id` con `hash()` aleatorio | — | Clave natural `(u, v, k)` + `blake2b` + adopción de filas antiguas (24 574/24 574 conservaron su `osm_id`). También se corrigió la lectura de listas de osmnx, cuyo orden dependía de `PYTHONHASHSEED` |
| P4 | ✅ **Resuelto** — topología reconstruida a mano, sin `maxspeed` | — | `source/target = u/v`; vértices desde los nodos OSM; `maxspeed_kmh`, `oneway` |
| P5 | ✅ **Resuelto** — vértices sin PK ni GiST | — | Migración 005; el nodo más cercano usa `Index Scan using idx_vertices_geom` |
| P6 | ✅ **Resuelto** — `trafico_calles` sin unicidad | — | Migración 006; `timestamp_calculo` = referencia del ciclo; `ON CONFLICT DO NOTHING` + omisión de ciclos ya inferidos |
| P7 | ✅ **Resuelto** — índice duplicado en `trafico_calles` | — | Migración 007 (~2,9 MB liberados; sin duplicados exactos en toda la BD) |
| P8 | ✅ **Resuelto** — sin compresión/retención | — | Migración 008; jobs 1000–1002 de TimescaleDB |
| P9 | ✅ **Resuelto** — tres BBOX distintos (302 de 475 puntos TomTom fuera de la red OSM) | — | Zona única en `src/zona.py` (la amplia, incluye Soledad); red OSM ampliada a 67 464 aristas; 35 puntos TomTom que estaban fuera de toda la zona eliminados (475 → 440) y filtro en `generador_topologia.py` |
| P10 | `trafico_inferido` obsoleta | Ruido en el esquema | Eliminar tras migrar paneles de Grafana |
| P11 | Auto-lazos (`source = target`) en `dim_calles_osm` (29 en la red original) | Inofensivos para Dijkstra, pero ruido | Filtrar en la vista de costos |
