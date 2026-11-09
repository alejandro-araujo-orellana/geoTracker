# Diseño: Ruteo con tráfico real (Dijkstra sobre pgRouting)

> Estado: **diseño / próxima fase**. El grafo y el encaje de coordenadas a nodos ya funcionan en la BD
> (prueba 2026-11-09: (22.1515, -100.9757) → (22.1300, -101.0050) = 54 aristas, 5 029 m, distancia
> pura). Falta el costo basado en tráfico y la API.

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
TomTom ─▶ fact_lecturas_trafico ─▶ predictor (IDW) ─▶ trafico_calles (congestión por arista OSM)
                                                              │
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

## SQL de referencia

### Prerrequisitos
Ya aplicados (migraciones 004–005): PK + GiST en vértices, `source/target` = nodos OSM,
`maxspeed_kmh`, `oneway`, `activo`. Falta como migración nueva (006):
```sql
CREATE TABLE dim_velocidad_tipo_via (
    tipo_via        text PRIMARY KEY,
    velocidad_kmh   double precision NOT NULL
);
```

### Vista de costos (tráfico actual)
```sql
CREATE OR REPLACE VIEW v_costos_grafo AS
WITH ultimo_ciclo AS (
    SELECT max(timestamp_calculo) AS ts
    FROM trafico_calles
    WHERE timestamp_calculo > now() - interval '30 minutes'
)
SELECT
    c.id,
    c.source,
    c.target,
    (c.longitud_m / (COALESCE(c.maxspeed_kmh, v.velocidad_kmh) / 3.6))
        * COALESCE(t.nivel_congestion_inf, 1.0)  AS cost,
    -1::double precision                          AS reverse_cost
FROM dim_calles_osm c
JOIN dim_velocidad_tipo_via v USING (tipo_via)
LEFT JOIN trafico_calles t
       ON t.osm_id = c.osm_id
      AND t.timestamp_calculo = (SELECT ts FROM ultimo_ciclo)
WHERE c.activo
  AND c.source <> c.target;
```

### Encajar una coordenada al vértice más cercano
```sql
SELECT id
FROM dim_calles_osm_vertices_pgr
ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
LIMIT 1;
```
(Mejora futura: `pgr_withPoints` para iniciar a mitad de una arista.)

### Ruta más rápida
```sql
SELECT r.seq, r.edge, c.nombre, c.tipo_via, c.longitud_m,
       r.cost AS segundos, r.agg_cost AS segundos_acumulados, c.geom
FROM pgr_dijkstra(
       'SELECT id, source, target, cost, reverse_cost FROM v_costos_grafo',
       :vertice_origen, :vertice_destino, directed => true) r
JOIN dim_calles_osm c ON c.id = r.edge
ORDER BY r.seq;
```

## Plan de implementación

1. **Saneamiento previo** (`DATABASE.md`): ✅ P1–P5 (2026-11-09). Pendiente P6 (unicidad en `trafico_calles`).
2. **Migración de ruteo (006):** tabla `dim_velocidad_tipo_via` + vista `v_costos_grafo`.
3. **Módulo `src/router.py`** (o servicio en `services/api`): `ruta_mas_rapida(origen, destino) -> Ruta` con snap + Dijkstra + suma de distancia/ETA; GeoJSON de salida.
4. **Endpoint** `GET /api/v1/rutas?origen=lat,lon&destino=lat,lon` en `services/api` y añadir el servicio a `docker-compose.yml`.
5. **Validación:** comparar ETA contra TomTom Routing API en una muestra de pares O/D; ajustar velocidades libres.
6. **Futuro:** A* (`pgr_aStar`) para acelerar; costos predichos por hora del día a partir del histórico en `fact_lecturas_trafico`; penalización de giros.

## Limitaciones conocidas
- La congestión de calles residenciales es **inferida** por IDW desde las avenidas medidas, no medida.
- El IDW recorta la congestión a [1, 3]; congestiones extremas (observadas hasta 8) quedan subestimadas.
- Si el ETL no corre (p. ej. TomTom 403), la vista cae a flujo libre (1.0) y la ruta deja de reflejar el tráfico.
