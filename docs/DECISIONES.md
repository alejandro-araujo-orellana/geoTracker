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
**Estado:** implementado antes de la fase 2 para probarlo en local y en Compose — ver D26. Falta TLS en la nube.
**Dónde:** `08-ingress.yaml` en el commit `8461992`; `docs/KUBERNETES.md` › Fase 2.

## D24 — ETL en modo un ciclo (`--una-vez`) para el CronJob (2026-11-12)
**Decisión:** `python src/main.py --una-vez` ejecuta un `ciclo_etl` y termina; sin la bandera sigue el daemon de
Compose (migraciones al arrancar + ciclo cada 15 min), sin cambios.
**Detalles:**
- `--una-vez` **no aplica migraciones**: en Kubernetes las aplica una sola vez el `Job` `geotracker-migrate`;
  hacerlo cada 15 min desde el CronJob solo abriría carreras de DDL.
- Exit 1 si el ciclo lanza una excepción (el Job queda fallido, con su log); exit 0 en lo demás, incluido
  "sin datos" por falta de cuota y una pasada de tiles fallida que el ciclo ya absorbe (D12). Un fallo total de
  TomTom se detecta por métricas (fase 3), no por el exit code.
- `main(argv)` es testeable: 3 tests nuevos en `tests/test_main.py`.

## D25 — Volumen de la BD en Kubernetes: montar el padre de PGDATA + `fsGroup` (2026-11-12)
**Decisión:** el PVC se monta en `/home/postgres/pgdata` y `PGDATA=/home/postgres/pgdata/data` (la misma ruta de
datos que Compose y que la imagen). El pod corre como `1000:1000` con `fsGroup: 1000` y
`fsGroupChangePolicy: OnRootMismatch`.
**Por qué:** montar el PVC directamente en `PGDATA` (como Compose) **falla**: un PVC llega con dueño root y `initdb`
no puede hacer `chmod 0700` ("could not change permissions ... Operation not permitted"). Compose no tiene el
problema porque un volumen nombrado de Docker copia el dueño del directorio de la imagen. Probado con la imagen
fijada sobre un directorio root:0777 (como `local-path` de k3d): montaje en `PGDATA` → falla; montaje en el padre →
arranca. En Azure Disk el disco llega root:0755 con `lost+found`, así que además hace falta `fsGroup` para que
`postgres` pueda crear `data/`. `local-path` ignora `fsGroup` (es hostPath), pero su 0777 basta.

## D26 — Token de acceso en la API: `X-API-Key` (2026-11-12)
**Decisión:** todo `/api/v1/*` exige el header `X-API-Key` con uno de los tokens de `API_TOKENS` (lista separada por
coma, uno por dispositivo); si falta o no coincide → 401 con `WWW-Authenticate: ApiKey`. `/health` y `/docs` quedan
abiertos. Implementa D23 desde ahora, en Compose y en Kubernetes.
**Detalles:**
- **Falla cerrada:** sin tokens, o con alguno de < 32 caracteres, la configuración no se construye y la API no
  arranca. Solo `API_AUTH_DESACTIVADA=true` la deja abierta (desarrollo; lo avisa en el log).
- Comparación con `secrets.compare_digest` contra **todos** los tokens (sin cortar en el primero que coincide).
  El token recibido nunca se registra.
- La dependencia va en `include_router(..., dependencies=[...])`: cualquier router nuevo bajo `/api/v1` queda
  protegido sin acordarse de ponerla. `APIKeyHeader` hace que `/docs` muestre el botón "Authorize".
- `/health` abierto porque lo llaman los probes de Kubernetes y el healthcheck de Compose, que no mandan token.
  `/docs` solo expone el esquema, no datos; puede cerrarse en la nube si se quiere.
- Revocar un dispositivo = quitar su token de la lista y reiniciar la API.
**Verificado:** 11 tests nuevos (`services/api/tests/test_seguridad.py`, sin BD) + los 12 de rutas con el header;
en vivo en Compose: sin token 401, token incorrecto 401, correcto 200, `/health` 200.
**Uso:** `curl -H "X-API-Key: $TOKEN" 'localhost:8000/api/v1/rutas?...'` (el token está en `.env`).

## D27 — Secretos de Kubernetes fuera de `base/`: `secretGenerator` desde un archivo no versionado (2026-11-12)
**Decisión:** se elimina `base/02-secret.yaml`. En local, el `Secret` `geotracker-secret` lo genera Kustomize desde
`overlays/local/secret.env` (en `.gitignore`; plantilla versionada `secret.env.example`), con
`disableNameSuffixHash` para conservar el nombre fijo. En la nube lo aportará Sealed/External Secrets (fase 2).
**Por qué:** un `Secret` en `base/` solo tenía dos salidas malas: con valores vacíos, cada `kubectl apply -k`
pisaba el secreto real creado por comando; con los valores reales, el archivo (ya versionado en `8461992`) acababa
en git y en GitHub. El generador lee un archivo que git no ve, y el `apply -k` sigue siendo un solo comando.
**Consecuencias:** el overlay declara su propio `namespace` (el de la base no alcanza a lo que genera el overlay).
Con el nombre fijo, cambiar un valor no reinicia los pods: `kubectl rollout restart`. Los valores que estaban en
`02-secret.yaml` se movieron a `secret.env` y se verificó que coinciden con `.env` y con el `Secret` renderizado.

## D28 — Primer despliegue en k3d: service links, CronJob suspendido e Ingress (2026-11-13)
**Decisiones:**
- **PgBouncer con `enableServiceLinks: false` y `PGBOUNCER_PORT` explícito.** Kubernetes inyecta variables por cada
  Service del namespace; el Service `pgbouncer` genera `PGBOUNCER_PORT=tcp://10.43.x.x:6432`, que choca con la
  variable homónima de la imagen de Bitnami ("value is not an integer") → `CrashLoopBackOff`, y en cascada fallaban
  migraciones y ETL (`Connection refused`).
- **CronJob del ETL suspendido en `overlays/local`** (parche `suspend: true`, declarativo para que `apply -k` no lo
  reactive). Mientras corre el ETL de Compose, los dos gastarían la misma clave de TomTom con pools `consumo_api`
  separados (~282 000 de 200 000 tiles/mes), y la BD del cluster aún no tiene la red OSM. Se quita al hacer el corte
  (fase 4, deteniendo antes el ETL de Compose). Prueba manual: `kubectl create job --from=cronjob/geotracker-etl`.
- **Ingress con `spec.ingressClassName: traefik`** en lugar de la anotación obsoleta `kubernetes.io/ingress.class`.
  En AKS la clase será otra y la parchea `overlays/nube`.
**Verificado:** pods `Running`, Job de migraciones `Complete` (001–012), `/health` ok vía PgBouncer, `apply -k` sin
warnings; por `port-forward`: sin token 401, con token 503 "La red vial no está cargada" (esperado: falta cargar OSM).
**Nota operativa:** un `Job` fallido no se re-ejecuta: `kubectl delete job geotracker-migrate` y volver a `apply -k`.

## D29 — Red OSM como `Job` en Kubernetes y corte del ETL de Compose al `CronJob` (2026-11-13)
**Decisiones:**
- **`base/09-osm-loader-job.yaml`**: `scripts/osmnx_loader.py` corre como `Job` con la imagen del ETL (el
  `Dockerfile` ahora copia `scripts/`; se agrega `.dockerignore` para no meter `.env`, `cache/` ni `.git` en el
  contexto). Un `initContainer` espera a que `schema_migrations` llegue a 005, así un solo `apply -k` sirve aunque
  el `Job` de migraciones no haya terminado. Va directo a `geotracker-db:5432`, no por PgBouncer: es una carga
  masiva en una sola transacción y no gana nada con el pool por transacción.
- **Caché de osmnx en un PVC (`cache-osmnx`, 1 Gi) sembrado desde `./cache` del host.** El Overpass público
  (`overpass-api.de`) rechaza clientes automatizados: responde un aviso de "monta tu propia instancia" en lugar de
  JSON (los 3 intentos del primer `Job` fallaron así). Con la caché (mismo bbox y misma versión de osmnx) la carga
  tardó 27 s y dio la misma red que Compose: 67 464 aristas, 28 008 nodos, 1 componente, 0 aristas sin vértice.
  Sembrado: `deploy/k8s/tools/sembrar-cache-osmnx.yaml` + `kubectl cp`. Para *actualizar* la red hará falta un
  Overpass que acepte la carga (instancia propia) o correr el loader en el host y volver a sembrar la caché.
- **ETL: se apaga el de Compose y se activa el `CronJob`** (se quita el parche `suspend` de D28). El servicio
  `etl_worker` queda detrás del perfil `etl-compose`, para que un `docker compose up -d` no lo reviva y duplique la
  cuota; arrancarlo exige `--profile etl-compose` y suspender antes el `CronJob`.
- **Se copia `consumo_api` del mes a la BD del cluster** antes del primer ciclo. Sin esto el `CronJob` partiría de
  cero y creería tener la cuota mensual entera (Compose ya llevaba 8 282 tiles y 1 839 Segment Data en noviembre).
**Verificado:** primer ciclo programado (17:15 UTC) `Complete` en 16 s: presupuesto "mes 7106/200000 antes de hoy",
49/49 tiles, 14 997 aristas medidas + 52 467 por IDW en `trafico_calles`. `GET /api/v1/rutas` con token devuelve
una ruta real.
**Consecuencia — datos partidos:** el histórico (desde 2026-11-09) sigue en la BD de Compose y los ciclos nuevos van
a la del cluster. Grafana sigue leyendo la de Compose, así que su mapa de calor queda congelado en el último ciclo
de Compose (17:02 UTC). Pendiente de la fase 4: llevar el histórico al cluster (`pg_dump`/`pg_restore` de datos) y
desplegar Grafana en el cluster o apuntarlo a su BD.

## D30 — Red OSM desde un extracto `.osm.pbf` en lugar de Overpass (2026-11-13)
**Contexto:** el Overpass público rechaza clientes automatizados (D29). La caché sembrada desde el host servía para
la carga inicial, pero congelaba la red en la fecha de la caché y no permitía actualizarla.
**Decisión:** `osmnx_loader.py` lee por defecto un extracto `.osm.pbf`. Fuente: `OSM_PBF_URL`, por defecto el
estado de San Luis Potosí en openstreetmap.fr (~15 MB, regenerado a diario; alternativa: México completo en
Geofabrik). Lo hace `scripts/fuente_pbf.py` con pyosmium (`osmium==4.0.2`). Solo se reemplaza la *descarga* de
osmnx: el `.pbf` se convierte en la misma respuesta JSON que daría Overpass y `ox.graph_from_bbox` hace el resto
sin cambios (búfer de 500 m, truncado, componente mayor, simplificación). La selección replica la consulta de
osmnx:
- el filtro `drive` se lee del propio osmnx (`_get_network_filter`) y se evalúa con la semántica de Overpass;
- se toman las vías con algún nodo dentro del polígono, con todos sus nodos;
- los elementos van ordenados por id y las coordenadas con 7 decimales.
Usa internals de osmnx fijados por versión (`osmnx==2.1.0`); al subir osmnx hay que repetir la validación.
`--fuente overpass` conserva el camino anterior (Overpass o su caché) y `--pbf archivo` usa un extracto local.
**Validación:** mismo bbox, grafo desde la caché de Overpass (2026-11-09) contra grafo desde el extracto
(datos 2026-11-13T00:49Z): 28 162 nodos y 67 914 aristas en ambos, 0 diferencias en `(u,v,k)` y 0 en
`osmid`/`highway`/`name`/`oneway`/`maxspeed`/`length`/geometría. En k3d el `Job` tardó 53 s: 0 aristas nuevas,
67 464 actualizadas, 0 inactivas, 0 aristas sin vértice, 1 componente.
**Consecuencias:** el `Job` ya no necesita el PVC `cache-osmnx` ni el pod de sembrado (se eliminan); usa un
`emptyDir` y necesita salida a internet hacia el servidor del extracto. Actualizar la red = borrar el `Job` y
volver a `apply -k`. Tests: `tests/test_fuente_pbf.py`.

## D31 — Observabilidad: Prometheus para operación, TimescaleDB para negocio, Grafana único (2026-11-13)
**Contexto:** fase 3 de `KUBERNETES.md`. Grafana sigue en Compose leyendo la BD de Compose (congelada desde el corte de
D29), sin provisioning (dashboards y datasource solo existen en el volumen `geotracker_grafana_data`), y no hay
métricas ni alertas: el 2026-11-13 el ETL estuvo ~4 h sin correr (equipo apagado) y nada lo avisó.
**Decisiones** (cierran las preguntas 9 y 10 de `KUBERNETES.md`):
- **Dos fuentes, separadas por tipo de dato.** Negocio (congestión, mapa de calor, ETAs) se queda en TimescaleDB;
  operación (latencia y errores de la API, éxito del ETL, reinicios, disco, cuota) va a Prometheus. La congestión
  **no** se exporta a Prometheus: ~67 k `osm_id` serían ~67 k series y el histórico ya vive en TimescaleDB.
- **`kube-prometheus-stack` por Helm con `values` versionados** (`deploy/k8s/platform/`), reducido en local (una
  réplica, retención 7 días). No contradice D20: Kustomize sigue a cargo de las apps de geoTracker; el chart es
  plataforma, como Traefik. En la nube lo instala Terraform (D18).
- **Grafana único: el del chart**, con datasources TimescaleDB + Prometheus y dashboards como ConfigMaps (sidecar
  de provisioning). Los dashboards de Compose se exportan a JSON y se versionan. El de Compose se retira después
  de la fase 4 (migración del histórico).
- **Sin Pushgateway.** El ETL vive ~20 s y su estado ya va a la BD (D7). Salud del job: `kube-state-metrics`
  (`kube_cronjob_status_last_successful_time`, incluido en el chart). Detalle del ciclo: tabla `ejecuciones_etl`
  (una fila por ciclo), que se grafica desde TimescaleDB y sobrevive a la retención de Prometheus.
- **Grafana entra a la BD con un rol de solo lectura** (`grafana_ro`), directo a `geotracker-db:5432`: no con el
  usuario dueño del esquema, y no por PgBouncer en modo transacción (prepared statements).
- **Alertas: un solo juego de reglas, motor de notificación por entorno.** Las reglas se escriben una vez como
  reglas de Grafana provisionadas (pueden consultar Prometheus *y* SQL: cuota en `consumo_api`, frescura de
  `trafico_calles`). Local: notifica el Alertmanager interno de Grafana (Grafana Alerting); el Alertmanager del
  chart va desactivado para ahorrar memoria. Nube (AKS): Alertmanager del chart activo; Grafana le envía sus
  alertas como Alertmanager externo, y ahí llegan también las reglas por defecto del chart (nodos, PVC,
  reinicios). La diferencia entre entornos queda en los overlays/`values` (`alertmanager.enabled` y el destino de
  las alertas de Grafana), no en las reglas.
**Por qué no dos juegos de reglas** (Grafana en local, `PrometheusRule` en la nube): divergen con el tiempo, y lo
que se prueba en local deja de ser lo que corre en la nube. Además, Prometheus no puede consultar SQL: las
alertas de cuota y frescura exigirían un exportador extra solo para la nube.
**Pendiente de verificar al escribir `overlays/nube`:** el envío de alertas de Grafana a un Alertmanager externo
(datasource de tipo Alertmanager + elección de Alertmanager en la configuración de Grafana) con la versión de
Grafana que traiga el chart en ese momento.

## D32 — Sondas separadas (`/health/live`, `/health/ready`) y `/metrics` en la API (2026-11-13)
**Contexto:** las sondas de Kubernetes solo miran el código HTTP, y `/health` responde **200 siempre**, también con
`"status": "degraded"`: las dos sondas apuntaban ahí y nunca detectaban una BD caída.
**Decisiones:**
- **`/health/live`** (liveness) no toca la BD: si la BD cae, reiniciar la API no arregla nada y reiniciaría todas
  las réplicas a la vez. **`/health/ready`** (readiness) consulta la BD y responde **503** si falla: el Service saca
  la réplica de la rotación sin matarla. `/health` se conserva igual (informativo). El healthcheck de Compose pasa
  a `/health/ready`.
- **`/metrics`** con `prometheus-fastapi-instrumentator==7.1.0` + `prometheus-client==0.21.1` (la 8.x exige
  Starlette ≥ 1.0, incompatible con `fastapi==0.115.6`). Sin token, igual que `/health`. Etiqueta por plantilla de
  ruta (nunca por la URL con su query) y excluye las sondas y el propio scrape.
- **Un worker de uvicorn por contenedor** (antes 2): con varios procesos cada uno tiene sus contadores y cada
  scrape vería uno al azar. Se escala con réplicas del Deployment; las rutas son `def` (threadpool), así que un
  worker atiende varias peticiones mientras esperan a la BD.
- **El Ingress publica solo `/api`, `/docs` y `/openapi.json`** (antes `/` completo). `/metrics` y `/health/*`
  quedan internos: Prometheus y el kubelet llegan por el Service y el pod.
- El `ServiceMonitor` se agrega con el stack (D31): su CRD aún no existe en el cluster.
**Verificado (k3d, 2026-11-13):** tests de la API 29/29 (`tests/test_observabilidad.py` nuevo; los de ruteo contra BD
real). Por el Service: `/health/live`, `/health/ready`, `/metrics` y la ruta con token → 200, y `http_requests_total`
etiquetado `handler="/api/v1/rutas"`. Por el Ingress: `/metrics`, `/health`, `/health/ready` → 404; `/docs`,
`/openapi.json` y la ruta con token → 200; sin token → 401. Con PgBouncer escalado a 0: `/health/ready` 503,
`/health/live` 200, la réplica pasa a `notReady` en los endpoints y **0 reinicios**; al restaurarlo vuelve a
`ready` sola.

## D10 — Imagen del ETL reproducible (2026-11-09)
**Decisión:** `Dockerfile` de una sola etapa sobre `python:3.11.15-slim-trixie` y todas las dependencias con
versión exacta (las que tenía la imagen en uso).
**Por qué:** el `Dockerfile` tenía dos `FROM` (el segundo, la imagen de la BD) y no podía construirse; la
imagen en uso venía de una versión anterior. Hacía falta reconstruirla para agregar `mapbox-vector-tile`.
La imagen previa se conserva localmente como `geotracker_etl_worker:anterior`.
