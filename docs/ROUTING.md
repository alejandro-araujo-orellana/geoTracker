# Diseño: Ruteo con tráfico real (Dijkstra sobre pgRouting)

> Estado (2026-11-09): **ruteo con tráfico implementado en la BD** (migración 011, D14):
> `SELECT * FROM ruta_mas_rapida(lat_o, lon_o, lat_d, lon_d)`. Prueba real Centro Histórico → Zona
> Universitaria UASLP con el tráfico de las 21:21 UTC (15:21 local): 79 aristas, 5.03 km, **10.4 min**;
> la misma ruta a flujo libre serían 7.6 min (~2.8 min perdidos por congestión); 0.59 s de cálculo.
> Falta: exponerlo en la API (`services/api`) y validar ETAs.

## Objetivo

Dada la ubicación del usuario y un destino en San Luis Potosí, devolver la **ruta más rápida
considerando el tráfico presente**, no la más corta. Además del mapa de calor actual
(`trafico_calles` en Grafana), el sistema podrá responder:

- ¿Cuál es la ruta más rápida de A a B **ahora**?
- ¿Cuánto tarda (ETA) y cuánta distancia recorre?
- ¿Cuánto tiempo se pierde por congestión frente al flujo libre?

## Por qué PostGIS en lugar de JSONB

La regla original ("geometría solo en JSONB") se reemplazó porque Dijkstra requiere:
1. Topología (`source`/`target`), que pgRouting construye a partir de geometrías PostGIS.
2. Búsquedas espaciales indexadas (nodo más cercano a un punto, `<->` con GiST).
3. Uniones espaciales entre segmentos TomTom y aristas OSM.

JSONB (`dim_segmentos_viales.coordenadas_geometria`) queda solo como copia cruda de la respuesta de TomTom.

## Flujo completo

```
TomTom tiles ─▶ fact_flujo_aristas ─┬─▶ predictor v3 ─▶ trafico_calles (congestión por arista OSM)
                                    └─ cerradas ──────────────┐       │
                                                              ▼
                                          v_costos_grafo (costo = tiempo con tráfico)
                                                              │
  (lat,lon) origen ──snap──▶ vértice A                         │
  (lat,lon) destino ─snap──▶ vértice B ──▶ pgr_dijkstra(v_costos_grafo, A, B, directed)
                                                              │
                                                              ▼
                                    ruta: aristas + geometría + ETA + distancia
```

## Función de costo

`nivel_congestion` = `currentTravelTime / freeFlowTravelTime`, así que multiplica directamente el
tiempo en flujo libre:

```
tiempo_libre_s(arista) = longitud_m / (velocidad_libre_kmh(tipo_via) / 3.6)
costo_s(arista)        = tiempo_libre_s × congestion_actual(arista)
congestion_actual      = último nivel_congestion_inf de trafico_calles para ese osm_id,
                         o 1.0 si no hay dato reciente (> 30 min)
```

Velocidades libres iniciales por tipo de vía (ajustables; idealmente reemplazar por `maxspeed`
de OSM o por `freeFlowSpeed` de TomTom cuando la arista esté medida):

| tipo_via | km/h |
|---|---|
| motorway, motorway_link | 80 / 50 |
| trunk, trunk_link | 70 / 40 |
| primary, primary_link | 50 / 35 |
| secondary, secondary_link | 45 / 30 |
| tertiary, tertiary_link | 40 / 30 |
| unclassified | 30 |
| residential | 25 |

### Direccionalidad
El grafo es dirigido: cada calle de doble sentido ya está duplicada con geometría invertida.
Por lo tanto **`reverse_cost = -1`** y `directed => true`. Ver `DATABASE.md › dim_calles_osm`.

## Implementación (migración 011)

Todo vive en `db/migrations/011_ruteo.sql`:

| Objeto | Qué hace |
|---|---|
| `dim_velocidad_tipo_via` | Velocidad libre por tipo de vía (tabla de arriba). Ajustable con `UPDATE` |
| `v_costos_grafo` | `id, source, target, cost (segundos), reverse_cost (-1)` con el tráfico del último ciclo (< 30 min) |
| `ruta_mas_rapida(lat_o, lon_o, lat_d, lon_d)` | Encaja cada coordenada al nodo más cercano (GiST) y corre `pgr_dijkstra` sobre la vista. Devuelve `seq, osm_id, nombre, tipo_via, longitud_m, segundos, segundos_acumulados, geom` |

Diferencias con el diseño original (D14):
- **Vías cerradas excluidas** del grafo (última pasada de tiles), no solo encarecidas.
- **`LEFT JOIN`** a `dim_velocidad_tipo_via` con 20 km/h por defecto: un `tipo_via` no listado (p. ej.
  `living_street`) ya no hace desaparecer la arista en silencio.
- **`maxspeed` de OSM solo si está entre 10 y 130 km/h**; si no, la velocidad del tipo de vía.
- Sin tráfico reciente (> 30 min) la vista usa flujo libre: la ruta sigue siendo válida, sin tráfico.

```sql
-- Ejemplo: Centro Histórico → Zona Universitaria UASLP
SELECT * FROM ruta_mas_rapida(22.1497, -100.9739, 22.1460, -101.0130);
```

Limitaciones del encaje: se usa el **nodo** más cercano (no un punto a mitad de calle); si ese nodo solo es
alcanzable a contrasentido, la función devuelve vacío. Mejora futura: `pgr_withPoints`.

Pruebas: `tests/test_ruteo_db.py` (BD temporal con grafo sintético): ruta corta sin tráfico, desvío con
atasco, vía cerrada excluida, tráfico viejo ignorado, sentido único respetado, `maxspeed` absurdo
descartado, tipo de vía desconocido conservado.

## Plan de implementación

1. **Saneamiento previo** (`DATABASE.md`): ✅ P1–P9 (2026-11-09).
2. **Migración de ruteo (011):** ✅ `dim_velocidad_tipo_via`, `v_costos_grafo` y `ruta_mas_rapida()` (2026-11-09).
3. **Capa Python / API** (`services/api`): llamar a `ruta_mas_rapida()` y devolver GeoJSON + distancia + ETA + minutos perdidos por congestión (comparando contra el costo a flujo libre).
4. **Endpoint** `GET /api/v1/rutas?origen=lat,lon&destino=lat,lon` en `services/api` y añadir el servicio a `docker-compose.yml`.
5. **Validación:** comparar ETA contra TomTom Routing API en una muestra de pares O/D; ajustar velocidades libres.
6. **Futuro:** A* (`pgr_aStar`) para acelerar; costos predichos por hora del día a partir del histórico en `fact_lecturas_trafico`; penalización de giros.

## Fuentes de tráfico y cuota de TomTom

Cuotas gratuitas **mensuales** (cuenta actual, 2026-11): Flow Segment Data **20 000**; Traffic Flow vector
tiles **200 000**. Con 440 puntos cada 15 min el ETL consumía ~42 000 peticiones/día: la cuota del mes se
agotaba en horas (los HTTP 403 de junio-julio).

**B.1 — Deduplicación** (`scripts/deduplicar_puntos.py`): 440 → 207 puntos. Con `zoom=10` TomTom devuelve
segmentos largos y hasta 12 puntos caían en el mismo.

**B.2 — Pool de peticiones** (`src/presupuesto.py` + tabla `consumo_api`):
- Presupuesto del día = (cuota × 0.9 − usado antes de hoy) / días restantes del mes (~600–820/día).
- Cupo del ciclo = presupuesto del día × peso(hora local) / Σ pesos del día: pico 3 (~16 por ciclo),
  valle 1 (~5), madrugada 0.2 (~1). Lo no gastado se reparte en los días siguientes.
- Cada intento HTTP cuenta (reintentos incluidos); un 403 bloquea el resto del día.
- Puntos del ciclo: primero los nunca medidos, luego los de lectura más antigua (rotación).
- Verificado con un ciclo real: 16/16 peticiones, consumo registrado, inferencia de 67 464 calles.

Limitación: con ~800 peticiones/día cada punto se renueva cada ~3 h en pico y ~10 h en valle; el
predictor solo usa lecturas de las últimas 6 h, así que la cobertura en vivo es escasa (en el primer
ciclo, 75 % de las calles quedó fuera del alcance de una fuente).

**A — Prueba de vector tiles** (`scripts/prueba_vector_tiles.py`, 2026-11-09, zoom 14):

| Métrica | Resultado |
|---|---|
| Tiles por pasada sobre la zona | 49 (zoom 13: 16; zoom 15: 182) |
| Consumo cada 15 min todo el día | ~4 700/día ≈ 141 000/mes (< 200 000) |
| Tramos de tráfico por pasada | 5 267, con `traffic_level` = velocidad actual / libre (0–1) y `road_closure` |
| Aristas OSM cubiertas | 14 698 (22 %; 24 % de los km) |
| Cobertura motorway / trunk / primary | 98–99 % |
| Cobertura secondary / tertiary | 95 % / 80 % |
| Cobertura residential | 8 % (sigue necesitando IDW) |
| Coincidencia con Flow Segment Data (16 segmentos, ~10 min de diferencia) | diferencia media 0.052, máx 0.211; detectó la congestión de Av. Constitución (0.30 vs 0.32) |

**Resultado:** los tiles pasan a ser la fuente principal (D8). Pendiente: verificar que los términos de
TomTom permiten almacenar datos derivados de tiles.

### Implementación (fuente principal)

`main.ciclo_etl` en cada ciclo de 15 min:
1. **Tiles** (`ciclo_tiles`): pool propio (200 000/mes, todo o nada por pasada, D12) →
   `extractor_tiles.extraer` (49 tiles asíncronos) → `asociador.asociar_y_guardar` → `fact_flujo_aristas`.
2. **Segment Data** (`ciclo_segment_data`): fuente secundaria dentro de su pool (B.2).
3. **Predictor** si alguna fuente trajo datos.

Asociación **dirigida** (D9): cada línea del tile va a la arista OSM a < 12 m de su punto medio con el
**mismo rumbo** (< 35° sobre 360°). Validado con una pasada real:
- Aristas oneway: 8 668 asociadas con la regla dirigida vs 2 880 con los tramos invertidos (la calzada
  contraria de avenidas con camellón, que la regla dirigida evita).
- 3 059 pares de doble sentido medidos; en 32 % el nivel difiere por sentido.
- Cobertura: motorway/trunk/primary ~100 %, secondary 98 %, tertiary 84 %, residential 8 %; 15 037 aristas.
- Tiempo: pasada de tiles + asociación ≈ 6 s (D11).

**Predictor v3** (D13): aristas medidas → valor directo (`TILE`); resto → IDW desde las medidas abiertas
(`IDW_TILE`). En un ciclo real: 15 039 medidas + 52 425 inferidas, solo 1 % de calles sin fuente cercana
(con Segment Data era 72.5 %). Ciclo completo del ETL: 10.7 s.

**Implicaciones para el ruteo** (pendiente, migración 011):
- La vista de costos usa `trafico_calles.nivel_congestion_inf` del último ciclo, como estaba diseñado.
- Además debe **excluir** las aristas con `fact_flujo_aristas.cerrada` en la última pasada: una vía cerrada
  no es "lenta", es intransitable (en `trafico_calles` solo aparece con el tope 10).
- El tope de congestión ahora es 10 (antes 3): los atascos severos pesan lo que realmente cuestan.

## Limitaciones conocidas
- La congestión de calles residenciales es en su mayoría **inferida** (los tiles miden ~8 % de ellas).
- ~~El IDW recorta la congestión a [1, 3]~~ Resuelto en D13: tope 10 y valores medidos directos.
- Si el ETL no corre (p. ej. TomTom 403), la vista cae a flujo libre (1.0) y la ruta deja de reflejar el tráfico.
