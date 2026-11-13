# Registro de decisiones — geoTracker

Qué se decidió, por qué y qué alternativas se descartaron. Una entrada por decisión, la más reciente
al final. Cada entrada cita el commit o la migración donde se aplicó. Si una decisión se revierte,
no se borra: se agrega una nueva que la reemplaza.

---

## D1 — Geometría en PostGIS en lugar de JSONB (2026-06)
**Decisión:** las geometrías viven en columnas `geometry` de PostGIS; el JSONB de TomTom queda como respaldo.
**Por qué:** el ruteo con Dijkstra (pgRouting) necesita topología (`source/target`) y búsquedas
espaciales indexadas, imposibles sobre JSONB.
**Dónde:** commit `1f8c662`; reglas en `ARCHITECTURE.md` §4.

## D2 — Esquema versionado con migraciones SQL propias (2026-11-09)
**Decisión:** `db/migrations/NNN_*.sql` idempotentes + `src/migrate.py` (sin Alembic).
**Por qué:** gran parte del esquema se había creado a mano y no se podía reproducir. SQL plano porque casi
todo el DDL es específico de PostGIS/TimescaleDB/pgRouting, que un ORM no modela bien.
**Dónde:** commit `0f4d026` (P1).

## D3 — Identidad de aristas OSM por `(u, v, k)` (2026-11-09)
**Decisión:** clave natural del grafo de osmnx; `osm_id` nuevo con `blake2b`; las filas antiguas se adoptan por geometría.
**Por qué:** `hash()` de Python es aleatorio por proceso y re-ejecutar el loader duplicaba la red.
**Descartado:** borrar y recargar la red (rompía el histórico de `trafico_calles`).
**Dónde:** commit `0f4d026` (P3/P4).

## D4 — Timestamps derivados = timestamp del dato de origen (2026-11-09)
**Decisión:** `trafico_calles.timestamp_calculo` es la lectura más reciente usada, no `now()`.
**Por qué:** con `now()` la restricción única nunca detecta que un ciclo se repitió sobre los mismos datos.
**Dónde:** commit `e02d375` (P6).

## D5 — Retención: datos crudos nunca, derivados 1 año (2026-11-09)
**Decisión:** compresión a 7 días en las hypertables; retención de 365 días solo en `trafico_calles`.
**Por qué:** los datos crudos de TomTom son la base de costos históricos por hora y no se pueden
volver a obtener; los derivados se pueden recalcular. 90 días habría borrado ese mismo día todo el histórico.
**Dónde:** commit `bbf3aa5` (P8), migración 008.

## D6 — Zona de estudio única, la amplia (incluye Soledad) (2026-11-09)
**Decisión:** un solo bbox en `src/zona.py`; red OSM ampliada a esa zona (24 574 → 67 464 aristas).
**Por qué:** 302 de 475 puntos TomTom caían fuera de la red OSM. Se prefirió ampliar la red (cobertura de
rutas en la periferia) a recortar los puntos.
**Dónde:** commit `bbf3aa5` (P9).

## D7 — Pool mensual de peticiones (2026-11-09)
**Decisión:** presupuesto diario derivado de la cuota mensual, ponderado por hora (pico/valle/madrugada),
contando cada intento HTTP en `consumo_api`; puntos deduplicados (440 → 207).
**Por qué:** la cuota de Flow Segment Data es de 20 000/mes; el ETL la agotaba en horas.
**Descartado:** horarios totalmente aleatorios (en hora pico el ruteo necesita datos frescos y regulares).
**Dónde:** commit `671baef` (B.1, B.2), migración 009.

## D8 — Traffic Flow vector tiles como fuente principal de tráfico (2026-11-09)
**Decisión:** el tráfico en vivo se obtiene de los vector tiles (`relative`, zoom 14) y se guarda **por arista
OSM**; Flow Segment Data queda como fuente secundaria (calibración/validación, dentro de su pool).
**Por qué:** con 20 000 peticiones/mes Segment Data no puede dar cobertura en vivo. Los tiles tienen
200 000/mes; 49 tiles cubren la zona (cada 15 min ≈ 141 000/mes) y la prueba midió 14 698 aristas (98 %
de las vías principales) con diferencia media de 0.05 contra Segment Data.
**Implica:**
- Extractor nuevo de tiles con su propio pool en `consumo_api`.
- Tabla nueva de lecturas por arista; no se guarda la geometría de los tiles (es estable y voluminosa).
- Asociación tile → arista en PostGIS en cada ciclo, **dirigida** (ver D9).
- Predictor: aristas medidas con su valor directo; el IDW solo rellena las demás.
**Riesgo abierto:** confirmar en los términos de TomTom que se permite almacenar datos derivados de tiles.
**Dónde:** prueba en commit `c60a55d`; implementación en los commits siguientes.

## D9 — Asociación tile → arista dirigida (2026-11-09)
**Decisión:** una línea de tile se asocia a la arista OSM cercana (< 12 m) con **el mismo rumbo** (diferencia
< 35° sobre 360°), no solo paralela.
**Por qué:** en un tile del centro, las vías de doble sentido traen dos líneas paralelas de sentidos opuestos,
a 2–11 m entre sí, y en 37 de 50 pares tertiary con `traffic_level` distinto: el tráfico es por sentido, igual
que las aristas dirigidas de OSM. La prueba original (sin sentido) mezclaba ambos.

## D11 — Asociación tile → arista en forma de conjunto, con índice en la tabla temporal (2026-11-09)
**Decisión:** la asociación calcula una vez el punto medio y rumbo de cada arista en una tabla temporal
**indexada (GiST) y analizada**, y une por `ST_DWithin`; solo los pares cercanos calculan rumbo.
**Por qué:** la primera versión (`LATERAL ... ORDER BY <-> LIMIT 1`) y la segunda (JOIN sin índice ni
estadísticas en la tabla temporal) tardaban **más de 7 minutos** por pasada: el planificador hacía el
producto cruzado de 67 k aristas × 4 k tramos (~280 M pares) calculando rumbos. `EXPLAIN ANALYZE` mostró
que con el índice baja a **1.3 s** (3.6 s con la carga de tramos), el mismo resultado.
**Lección:** toda tabla temporal que participe en un JOIN espacial necesita índice + `ANALYZE`.

## D12 — Pasada de tiles: todo o nada, y la fuente principal no bloquea a la secundaria (2026-11-09)
**Decisión:** cada ciclo hace la pasada completa de tiles (49) solo si el presupuesto del día alcanza; un
fallo en tiles se registra y el ciclo sigue con Segment Data; el predictor corre si alguna fuente trajo datos.
**Por qué:** una pasada parcial dejaría zonas sin medir en el mismo instante. Con 200 000/mes el
presupuesto diario (~6 000–8 000) cubre los 96 ciclos (4 704 tiles), así que no hace falta ponderar por hora.

## D13 — Predictor v3: medido donde hay tiles, IDW solo para el resto (2026-11-09)
**Decisión:**
- Las aristas medidas en la última pasada de tiles (< 30 min) toman su valor directo:
  `congestión = 1 / traffic_level`, recortado a [1, 10] (`metodo_inferencia = 'TILE'`).
- Las demás se infieren por IDW usando como fuentes los centroides de las aristas medidas **no cerradas**
  (`'IDW_TILE'`). Una vía cerrada no debe "pintar" congestión en sus vecinas.
- Si no hay pasada reciente, se usa la v2 (Segment Data) como respaldo (`'IDW_OSM'`).
- Tope de congestión 10 (antes 3): la v2 recortaba en 3 y subestimaba atascos reales (Segment Data llegó a
  medir 8). Las vías cerradas toman el tope, pero el ruteo debe **excluirlas** usando
  `fact_flujo_aristas.cerrada`, no solo encarecerlas.
**Resultado (ciclo real):** 15 039 aristas medidas + 52 425 inferidas; solo 1 % de las calles sin fuente
cercana (antes 72.5 %). Distribución medida: 76 % en flujo casi libre (≥ 0.8), 18 atascos severos (< 0.1)
y 61 vías cerradas; ningún nivel 0 sin marca de cierre (no hay "sin dato" disfrazado de atasco).
**También:** `guardar_resultados` pasa a INSERT por lotes de SQLAlchemy con `RETURNING` (antes `iterrows` +
`text()` fila a fila: ~30 s; ahora ~3 s). `RETURNING` porque con inserción por lotes `rowcount` solo cuenta
el último lote. Ciclo completo: 37 s → 10.7 s.

## D14 — Ruteo en la base de datos: vista de costos + función `ruta_mas_rapida` (2026-11-09)
**Decisión:** la lógica de ruteo vive en PostgreSQL (migración 011): `v_costos_grafo` calcula el costo en
segundos con el tráfico del último ciclo y `ruta_mas_rapida(lat_o, lon_o, lat_d, lon_d)` encaja y corre
`pgr_dijkstra`. La API solo llamará a la función.
**Por qué:** pgRouting necesita el grafo como SQL de todos modos; tenerlo en una función la hace usable
desde Grafana, psql o la API sin duplicar lógica, y probable contra PostgreSQL real.
**Ajustes al diseño de `ROUTING.md`:**
- Vías cerradas **excluidas** (no solo con costo alto): con costo alto Dijkstra las usaría si no hay otra.
- `LEFT JOIN` a velocidades con 20 km/h por defecto: con `JOIN` una arista de tipo no listado desaparecía
  del grafo en silencio y podía dejar zonas inalcanzables.
- `maxspeed` de OSM solo entre 10 y 130 km/h (los datos de OSM traen valores absurdos ocasionales).
**Verificado:** 7 tests de integración con grafo sintético; ruta real Centro → UASLP en 0.59 s sobre
~67 300 aristas. Con el tráfico de las 21:21 UTC: 5.03 km / 10.4 min (a flujo libre la misma ruta: 7.6 min).
**Corrección:** el commit `e2b4461` reportó "5.16 km / 9.6 min a flujo libre". Era incorrecto: en ese
momento ya había un ciclo de tráfico de hace 12 min (< 30 min), así que la vista usó tráfico real.

## D15 — ETL encendido solo con `--no-deps` (2026-11-09)
**Decisión:** el ETL se levanta con `docker compose up -d --build --no-deps etl_worker`.
**Por qué:** `trafico_db` y `grafana_bi` se crearon con Compose 1.29 (imagen `geotracker_timescaledb`); Compose
v5 nombra la imagen `geotracker-timescaledb` y, sin `--no-deps`, **recrearía el contenedor de la BD**. El volumen
de datos persistiría, pero no hay razón para arriesgarlo. Pendiente: migrar los contenedores a Compose v5 en
una ventana controlada (respaldo previo) y declarar `restart: unless-stopped` en el ETL, para que un reinicio
de Docker no detenga la acumulación de datos.

## D16 — API de rutas delgada sobre la BD (2026-11-09)
**Decisión:** `GET /api/v1/rutas?origen=lat,lon&destino=lat,lon[&detalle=true]` (FastAPI, `services/api`). La API
solo valida, llama a `ruta_mas_rapida()` y da formato: distancia, duración con tráfico, duración a flujo libre de la
misma ruta, demora por tráfico, vigencia del tráfico, calles en orden y geometría GeoJSON.
**Por qué así:**
- El tiempo a flujo libre se extrajo a la vista `v_tiempo_libre` (migración 012) y `v_costos_grafo` la usa: una sola
  fórmula de velocidad para el ruteo y para la API.
- Cobertura por distancia a la red (> 300 m del nodo más cercano → 422), no por bbox: no duplica `zona.py` en otro
  servicio y refleja la red realmente cargada.
- Errores explícitos: 422 (formato, fuera de cobertura, origen = destino), 404 (sin ruta, p. ej. contrasentido).
- Usa la carpeta `app/rutas/` que ya existía en el esqueleto; `app/api/v1` solo agrupa routers.
**Verificado:** 12 tests contra PostgreSQL real (BD temporal); en vivo, Centro → UASLP en 0.29 s.

## D17 — Contenedores en Compose v5, imágenes fijas y `restart` (2026-11-09)
**Decisión:**
- `docker-compose.yml` para Compose v2+ (`name: geotracker`, sin `version`), credenciales solo desde `.env`,
  `restart: unless-stopped` en los 4 servicios, healthcheck de la BD y `depends_on: service_healthy`.
- **Imágenes fijas:** BD `geotracker-timescaledb:pg15.2-ts2.10.2` (la imagen existente, re-etiquetada) con
  `db/Dockerfile` anclado por digest a la base de 2023 y pgRouting `4.0.1`; Grafana `13.0.2` (la local).
- El ETL ya no monta el repo (`.:/app`): corre el código de su imagen.
**Por qué:**
- `pg15-latest` hoy trae versiones de TimescaleDB que no cargan una BD con la extensión 2.10.2 sin
  `ALTER EXTENSION ... UPDATE`; reconstruir la BD "por accidente" podía dejarla sin arrancar. Actualizar
  PostgreSQL/TimescaleDB/pgRouting queda como migración propia (respaldo + prueba).
- `restart` evita que un reinicio de Docker/WSL detenga la acumulación de datos.
- Sin el bind mount, lo que corre es exactamente lo construido (requisito para Kubernetes en el siguiente milestone).
**Migración ejecutada:** respaldo previo (`vialidades_slp_20261109_193205_pre_compose_v5.dump`); mismos volúmenes
(`geotracker_datos_postgres`, `geotracker_grafana_data`); extensiones idénticas; ningún conteo disminuyó (el ETL nuevo
sumó su primer ciclo). Reemplaza la restricción de D15: ya se puede usar `docker compose up -d` normal.

## D18 — Herramientas para el milestone de Kubernetes (preliminar, 2026-11-09)
**Decisión:** se usarán **Terraform** (infraestructura: cluster, red, registro, respaldos y BD si queda fuera del cluster)
y **Ansible** (configuración de hosts/VMs y procedimientos operativos como la actualización de TimescaleDB).
**Prometheus** (+ Alertmanager) es la opción de observabilidad a evaluar. **Puppet queda descartado.** CI/CD (Jenkins o
GitHub Actions) sigue abierto.
**Por qué:** cada herramienta cubre una capa distinta y no chocan mientras cada una administre un solo dominio
(Terraform no despliega las apps; Ansible no despliega contenedores; un solo camino de despliegue). Puppet se traslapa
con el modelo de contenedores, exige servidor y agentes, y entraría en conflicto con Kubernetes sobre los nodos;
Ansible cubre la preparación de hosts sin agentes.
**Dónde:** análisis completo en `docs/KUBERNETES.md` › Herramientas complementarias. Se confirma o ajusta en la
sesión del milestone.

## D19 — Nube: Azure (AKS), después del entorno local (2026-11-12)
**Decisión:** el orden es local primero (fase 1) y nube después (fase 2). El proveedor de nube es **Azure (AKS)**,
con el crédito gratuito de $100 USD como presupuesto. Registro de imágenes: Azure Container Registry o GHCR.
**Por qué:** aprender Kubernetes en local no consume crédito; el crédito de Azure cubre la fase 2 si el cluster se
apaga cuando no se usa. La BD gestionada (opción C) se descartó porque ningún servicio ofrece la combinación fijada
PostgreSQL 15.2 + TimescaleDB 2.10.2 + PostGIS + pgRouting (D17): en la nube la BD irá con CloudNativePG (opción B).
**Pendiente:** medir el consumo contra el crédito en la fase 2; `overlays/nube` y `deploy/terraform/` aún no existen.
**Dónde:** `docs/KUBERNETES.md` › Fase 2; manifiestos base en el commit `8461992`.

## D20 — Kustomize (base + overlays), no Helm (2026-11-12)
**Decisión:** los manifiestos se administran con **Kustomize**: `deploy/k8s/base` (lo común) + `overlays/local` y,
en la fase 2, `overlays/nube`, que solo parchan lo que difiere (réplicas, almacenamiento, Ingress, secretos).
**Por qué:** viene integrado en `kubectl` (`apply -k`), son YAML planos sin lenguaje de plantillas, y para un solo
proyecto con dos entornos no hace falta el empaquetado/versionado de charts de Helm. Helm queda para componentes de
terceros (p. ej. `kube-prometheus-stack`, fase 3).
**Consecuencia:** `03b-database-cnpg.yaml` vive en `base/` pero fuera del `kustomization.yaml` hasta instalar el
operador; el cambio A → B es intercambiar ese archivo en la lista.
**Dónde:** commit `8461992` (`deploy/k8s/base/kustomization.yaml`, `deploy/k8s/overlays/local/kustomization.yaml`).

## D21 — Cluster local con k3d (2026-11-12)
**Decisión:** el cluster de desarrollo es **k3d** (k3s dentro de contenedores Docker): 1 server + 2 agentes, cluster
`geotracker`. Las imágenes se cargan con `k3d image import` (sin registro) e `imagePullPolicy: IfNotPresent`.
**Por qué:** corre sobre el Docker que ya usa Compose, sin VMs; se recrea con un comando; trae Traefik (Ingress) y el
provisionador `local-path` (PVC) de fábrica. Varios nodos permiten practicar el reparto de réplicas (D22).
Terraform/Ansible se reservan para la nube.
**Ojo:** Compose nombra la imagen del ETL `geotracker-etl_worker`; los manifiestos usan `geotracker-etl`, hay que
re-etiquetarla antes de importarla (tabla en `docs/KUBERNETES.md` › Imágenes).
**Dónde:** commit `8461992`.

## D22 — PgBouncer propio, PodDisruptionBudget y reparto entre nodos (2026-11-12)
**Decisión:** tres añadidos al diseño original, para los dos objetivos del milestone (redundancia y multiusuario):
- **PgBouncer** como `Deployment` propio (2 réplicas en base, 1 en local), modo `transaction`, 20 conexiones reales a
  Postgres. Todo el tráfico a la BD pasa por el `Service` `pgbouncer:6432` (`DB_HOST` en el ConfigMap).
- **PodDisruptionBudget** de la API con `minAvailable: 1`: un drenaje de nodo nunca deja la API en cero.
- **topologySpreadConstraints** por `kubernetes.io/hostname` (`ScheduleAnyway`): las réplicas de la API en nodos
  distintos; en la nube podrá endurecerse a `DoNotSchedule`.
**Por qué:** cada réplica de API (2 workers de uvicorn × pool de 5 + 5) y el ETL abren su propio pool; con 3 réplicas
se acercan a `max_connections = 100`, y Postgres crea un proceso por conexión. Sin PDB ni reparto, la redundancia de 3
réplicas es solo teórica: pueden caer juntas en un drenaje o en la caída de un nodo.
**Notas:** la imagen `bitnami/pgbouncer` ya no está en Docker Hub; se usa `bitnamilegacy/pgbouncer:1.22.1` (sin
actualizaciones; revisar en la fase 2 o usar el `Pooler` de CloudNativePG). En local, con 1 réplica, el PDB bloquea
`kubectl drain` (no se puede desalojar el único pod): esperado.
**Dónde:** commit `8461992` (`04-pgbouncer.yaml`, `05-api.yaml`).

## D23 — API no pública: acceso por token (2026-11-12)
**Decisión:** la API no se publica (ni en stores ni como servicio abierto). En la nube, el acceso se limita a
dispositivos propios con un **token en un header** que valida la propia API (401 si falta o no coincide) y,
opcionalmente, restricción por IP y TLS (cert-manager) en el Ingress.
**Por qué:** mantiene el proyecto dentro de D8 (datos derivados de TomTom para uso propio/investigación, no
redistribución pública). El token en la aplicación es portable entre controladores de Ingress (Traefik en local,
el que se use en AKS), a diferencia de las anotaciones de autenticación de cada controlador.
**Estado:** **no implementado.** `services/api` aún no valida ningún token y `08-ingress.yaml` solo enruta; se
implementa en la fase 2, con el token como clave nueva del `Secret`.
**Dónde:** `08-ingress.yaml` en el commit `8461992`; `docs/KUBERNETES.md` › Fase 2.

## D10 — Imagen del ETL reproducible (2026-11-09)
**Decisión:** `Dockerfile` de una sola etapa sobre `python:3.11.15-slim-trixie` y todas las dependencias con
versión exacta (las que tenía la imagen en uso).
**Por qué:** el `Dockerfile` tenía dos `FROM` (el segundo, la imagen de la BD) y no podía construirse; la
imagen en uso venía de una versión anterior. Hacía falta reconstruirla para agregar `mapbox-vector-tile`.
La imagen previa se conserva localmente como `geotracker_etl_worker:anterior`.
