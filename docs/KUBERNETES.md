# Kubernetes — plan e implementación del milestone

> **Actualización 2026-11-17:** alertas como código: las 5 iniciales + "ETL sin datos de tiles" (D40, D41); tabla
> `ejecuciones_etl` (D41); dashboard rehecho sobre datos de tiles (D36–D38, D40) y memoria de PostgreSQL acorde al
> límite del pod (D39). Faltan un punto de contacto que notifique fuera de Grafana, `postgres_exporter` y el
> histórico (fase 4).
>
> **Actualización 2026-11-14:** fase 3 en curso en k3d. Ya están Prometheus + Grafana (`kube-prometheus-stack`), los
> dashboards como código, el rol `grafana_ro` y la API scrapeada (D31–D33); ver "Observabilidad: operación".
> Faltan `ejecuciones_etl`, las reglas de alerta y el histórico (fase 4).
>
> **Actualización 2026-11-13:** fase 1 aplicada en k3d (D28, D29): BD, PgBouncer, API con token, migraciones y
> red OSM cargadas; el ETL corre como `CronJob` y el de Compose está apagado. Falta el histórico y Grafana (fase 4).
>
> Estado: **Fase 1 en implementación** (actualizado 2026-11-12). El análisis previo (2026-11-09) se
> conserva abajo como fundamento. Las decisiones que estaban **abiertas** ya se tomaron en esta sesión y se
> reflejan aquí y en `docs/DECISIONES.md` (D19–D23). Los manifiestos `base` + `overlays/local` ya existen:
> renderizan con `kubectl kustomize` y pasan `kubectl apply -k --dry-run=client --validate=strict` contra el
> cluster k3d (13 objetos, 2026-11-12). **Aún no se han aplicado.** El modo `--una-vez` ya existe (D24); antes de
> aplicar hay que reconstruir `geotracker-etl:latest` e importarla a k3d. `overlays/nube` no existe todavía (fase 2).

## Objetivo

Desplegar geoTracker (BD, ETL, API, Grafana) sobre Kubernetes, con **dualidad local / nube**: los mismos
manifiestos deben poder correr en un cluster local (desarrollo) y en uno administrado en la nube (producción).
Dos objetivos concretos guían el diseño:

1. **Redundancia / tolerancia a fallos**: que la caída de un pod, un nodo o un despliegue nuevo no deje el
   servicio sin responder.
2. **Manejo de tráfico multiusuario**: repartir la carga de varias peticiones entre varias réplicas para que
   ninguna sea un cuello de botella único.

## Ubicación de los manifiestos

Todo lo de despliegue vive en `deploy/` en la raíz del repo, versionado junto al código que despliega (el
manifiesto de la versión X de la API viaja en el mismo commit que esa versión X):

```
geoTracker/
├── docker-compose.yml          # el stack de Compose sigue existiendo (desarrollo rápido)
├── db/ src/ scripts/ ...        # código actual
├── docs/
│   ├── DECISIONES.md
│   └── KUBERNETES.md            # este documento
└── deploy/
    ├── k8s/
    │   ├── base/                # manifiestos comunes a local y nube + kustomization.yaml
    │   │   ├── 00-namespace.yaml
    │   │   ├── 01-configmap.yaml
    │   │   ├── 03-database-statefulset.yaml   # BD opción A (local, fase 1)
    │   │   ├── 03b-database-cnpg.yaml         # BD opción B (CloudNativePG, fase 2)
    │   │   ├── 04-pgbouncer.yaml
    │   │   ├── 05-api.yaml               # Deployment + Service + PodDisruptionBudget
    │   │   ├── 06-etl-cronjob.yaml
    │   │   ├── 07-migrations-job.yaml
    │   │   ├── 08-ingress.yaml
    │   │   ├── 09-osm-loader-job.yaml     # red OSM desde extracto .osm.pbf (D29, D30)
    │   │   └── kustomization.yaml
    │   ├── platform/            # observabilidad (D31–D33)
    │   │   ├── kube-prometheus-stack/   # values.yaml + values-local.yaml del chart (Helm)
    │   │   ├── grafana/dashboards/      # dashboards JSON → ConfigMaps
    │   │   ├── servicemonitor-api.yaml
    │   │   └── kustomization.yaml       # lo incluyen los overlays
    │   └── overlays/
    │       ├── local/           # réplicas 1, parches; Secrets desde secret.env y grafana.env (fuera de git, D27, D33)
    │       └── nube/            # réplicas ≥ 2, Ingress + TLS + token, StorageClass de Azure (fase 2)
    ├── terraform/               # (fase 2) infraestructura de Azure
    └── ansible/                 # (fase 2, si aplica) preparación de hosts / actualización de TimescaleDB
```

Cada herramienta administra **un solo dominio** (D18): Terraform crea el cluster y la red; Kustomize despliega
las apps; Ansible prepara hosts y tareas operativas. No se pisan.

## Conclusión del análisis

**Es posible y factible.** El proyecto ya cumple la mayoría de los requisitos de una aplicación para
Kubernetes gracias a decisiones de la rama `feat/ruteo-dijkstra-postgis`:

| Requisito de Kubernetes | Estado actual | Dónde |
|---|---|---|
| Imágenes reproducibles con versión fija | ✅ | D10, D17 |
| Contenedores inmutables (sin montar el código) | ✅ | D17 |
| Configuración por variables de entorno | ✅ (`.env` → `Secret`/`ConfigMap`) | D17, fase 1 |
| Esquema versionado y aplicable de forma idempotente | ✅ `db/migrations` + `migrate.py` | D2 |
| Procesos idempotentes (repetir un ciclo no duplica) | ✅ | D4, P6 |
| Estado fuera del proceso (cuotas en la BD, no en memoria) | ✅ `consumo_api` | D7 |
| Sonda de salud de la API | ✅ `/health` | D16 |
| Logs a stdout | ✅ | — |

**La parte difícil es la base de datos** (sección propia abajo). **El costo real es operativo**: para la escala
actual (una ciudad, ciclo ETL de ~12 s cada 15 min, API ligera) Compose es suficiente; Kubernetes se justifica
por aprendizaje/portafolio, crecimiento (varias ciudades, más tráfico en la API) o alta disponibilidad.

## Decisiones tomadas en esta sesión (antes abiertas)

Estas cierran las preguntas del final del documento y se registran en `DECISIONES.md`:

- **Orden local → nube** (pregunta 1): local primero para aprender sin quemar crédito; nube después.
- **Proveedor de nube** (pregunta 2): **Azure** (AKS), aprovechando $100 USD de crédito gratuito. Presupuesto:
  el crédito; medir consumo en la fase 2, apagar el cluster cuando no se use.
- **Base de datos** (pregunta 3): **opción A (StatefulSet) en local para la fase 1; opción B (CloudNativePG)
  en la fase 2**. La actualización de TimescaleDB 2.10.2 se evalúa como prerrequisito de la fase 2, no "de paso".
- **Kustomize, no Helm** (pregunta 4): `base` + overlays; más simple para aprender y sin plantillas.
- **API no pública, acceso restringido** (pregunta 5): no se publica en ninguna store. En la nube, el acceso se
  limita a dispositivos propios por **token** (header que valida la API) y/o restricción por IP en el Ingress.
  Esto mantiene el proyecto del lado seguro de D8 (uso en investigación/desarrollo, no redistribución pública).
- **Añadidos al diseño original** (confirmados contra los dos objetivos):
  - **PgBouncer como Deployment propio**: evita agotar `max_connections` con varias réplicas de API + ETL.
  - **PodDisruptionBudget (`minAvailable: 1`)**: garantiza que un drenaje de nodo nunca deje la API en cero.
  - **topologySpreadConstraints**: reparte las réplicas de la API entre nodos distintos.
- **Cluster local: k3d** (pregunta 7): sobre Docker, sin VMs; Terraform/Ansible se reservan para la nube.

Siguen **abiertas** para fases posteriores: CI/CD Jenkins vs GitHub Actions (pregunta 6), detalles de Terraform
y `tfstate` remoto (pregunta 8) y el canal de alertas (pregunta 10). Grafana y Pushgateway (pregunta 9) y el
motor de alertas se decidieron en D31.

## Línea base medida (Compose, 2026-11-09)

Datos para dimensionar `requests`/`limits` y volúmenes. Volver a medir con varios días de datos.

| Componente | Memoria en reposo | CPU en reposo | Imagen | Notas |
|---|---|---|---|---|
| BD (`trafico_db`) | ~364 MiB | ~3 % | 4.53 GB | 307 MB de datos tras 1 día con tiles |
| ETL (`etl_trafico`) | ~153 MiB | ~0 % (picos en el ciclo) | 2.27 GB | Ciclo ~12 s cada 15 min; predictor con 67 k calles |
| API (`geotracker_api`) | ~131 MiB | <1 % | 302 MB | Ruta en ~0.3 s (pgr_dijkstra carga el grafo por petición) |
| Grafana (`grafana_bi`) | ~125 MiB | ~1 % | 1.47 GB | — |

Los `requests`/`limits` de los manifiestos salen de esta tabla, con margen para los picos del predictor.

Crecimiento de datos estimado (a verificar): `trafico_calles` ~6.5 M filas/día y `fact_flujo_aristas`
~1.4 M filas/día antes de compresión (a los 7 días). Con 1 día: `trafico_calles` 113 MB, `fact_flujo_aristas` 13 MB.

## Mapeo de componentes (implementado en `deploy/k8s/base`)

| Componente | Recurso de Kubernetes | Archivo | Notas |
|---|---|---|---|
| API | `Deployment` (3 réplicas en base, 1 en local) + `Service` + `PodDisruptionBudget` | `05-api.yaml` | Readiness en `/health/ready`, liveness en `/health/live` (D32). Spread entre nodos. Candidata a HPA |
| Pooler | `Deployment` (2 réplicas) + `Service` | `04-pgbouncer.yaml` | Todo el tráfico a la BD pasa por aquí (puerto 6432) |
| ETL | `CronJob` `*/15`, `Forbid`, `timeZone: America/Mexico_City` | `06-etl-cronjob.yaml` | Reemplaza el `while True`. `Forbid` = nunca cuota doble |
| Migraciones | `Job` explícito (no initContainer) | `07-migrations-job.yaml` | Corre una vez antes de la app; evita carreras entre réplicas |
| BD | `StatefulSet` + PVC (fase 1) → operador CloudNativePG (fase 2) | `03` / `03b` | Ver "Base de datos" abajo |
| `.env` | `Secret` + `ConfigMap` | `01` / `02` | Config no sensible en ConfigMap; credenciales en Secret |
| Entrada | `Ingress` | `08-ingress.yaml` | Traefik en local; TLS + token en la fase 2 |
| Carga de red OSM | `Job` (`osmnx_loader.py`) | `09-osm-loader-job.yaml` | Espera migraciones ≥ 005. Lee un extracto `.osm.pbf` (`OSM_PBF_URL`), no Overpass (D30); necesita salida a internet |
| Grafana + Prometheus | Chart `kube-prometheus-stack` (Helm), namespace `monitoring` | `platform/kube-prometheus-stack/` | Grafana 13.0.2 con PVC; datasources TimescaleDB (`grafana_ro`) y Prometheus (D33) |
| Dashboards + ServiceMonitor | `ConfigMap` por dashboard + `ServiceMonitor` | `platform/kustomization.yaml` | Incluido por el overlay; requiere los CRDs del chart |
| Respaldos | `CronJob` con `pg_dump` | (pendiente) | Hoy manuales; en fase 2 con el operador |

## Base de datos — decisión tomada (A → B)

Motor actual: imagen propia fijada `geotracker-timescaledb:pg15.2-ts2.10.2` (PostgreSQL 15.2, TimescaleDB 2.10.2,
PostGIS 3.3.2, pgRouting 4.0.1 — D17). Esta combinación **no** se ofrece tal cual en los servicios administrados
habituales, por eso se descartó la opción C de BD gestionada.

| Opción | A favor | En contra | Uso |
|---|---|---|---|
| A. `StatefulSet` simple con imagen propia | Igual en local y nube; control total de extensiones | Punto único de fallo; respaldos/failover propios | **Fase 1 (local)** |
| B. Operador CloudNativePG con imagen propia | Réplicas, failover y respaldos gestionados; dos Services read/write automáticos | Requiere imagen compatible; validar TimescaleDB + pgRouting | **Fase 2 (nube)** |
| C. BD fuera del cluster | Kubernetes solo para lo stateless | Depende de encontrar TimescaleDB + PostGIS + pgRouting gestionado | Descartada |

Por qué B cumple los objetivos y A no: con A (una réplica) la caída de la BD deja toda la API sin servicio
(toda la lógica de ruteo vive en la BD, D14). B levanta primaria + réplicas con failover automático, y crea
`geotracker-db-rw` (escritura, siempre la primaria) y `geotracker-db-ro` (lectura, reparte entre réplicas) —
exactamente lo que `database.py` espera con `DATABASE_WRITE_URL` / `DATABASE_READ_URL`.

**Prerrequisito de la fase 2:** planear la actualización de PostgreSQL 15.2 / TimescaleDB 2.10.2 (≈ 3 años) a
versiones soportadas, y **validar que la imagen fijada arranca bajo CloudNativePG** antes de confiar en `03b`.
Es una migración propia: respaldo, `ALTER EXTENSION ... UPDATE`, pruebas con copia, plan de vuelta atrás.
Recordar: TimescaleDB 2.10 no admite `ON CONFLICT` en chunks comprimidos (D5/P8); versiones más nuevas sí.

## Cambios de código previstos (antes o durante el milestone)

1. **Modo "un ciclo" en el ETL**: `python src/main.py --una-vez` (ejecuta `ciclo_etl` y termina, exit != 0 si
   falla) para el `CronJob`. El modo daemon se queda para Compose. — **hecho (D24)**: `--una-vez` no aplica
   migraciones (las aplica el `Job`); exit 1 solo si el ciclo lanza una excepción.
2. **Migraciones como comando aparte** (`python src/migrate.py`), fuera del arranque del ETL. — **requerido por
   `07-migrations-job.yaml`**
3. **Grafana como código**: datasource y dashboards en provisioning versionado. — fase 3
4. **Sondas**: el ETL como `CronJob` no necesita liveness. La API usa `/health/live` y `/health/ready` — **hecho
   (D32)**: `/health` responde 200 aunque la BD falle, así que no sirve como sonda.
5. **Apagado ordenado (SIGTERM)** en el ETL para no cortar un ciclo a la mitad (aunque es idempotente).
6. **Rendimiento de la API con réplicas**: `pgr_dijkstra` carga el grafo completo por petición; con más tráfico,
   evaluar `pgr_aStar`, limitar el grafo por bbox origen-destino, o caché de rutas. Es el techo real de escala,
   no el número de pods.
7. **Pool de conexiones**: resuelto con **PgBouncer** como Deployment propio (`04-pgbouncer.yaml`).

## Plan por fases

### Fase 1 — Entorno base en Kubernetes local (en curso)

**Entregable:** cluster local k3d + manifiestos `base` + `overlays/local` corriendo, con:

- **BD como `StatefulSet`** (opción A) con PVC: `03-database-statefulset.yaml`.
- **Secrets** (D27): el `Secret` no vive en `base/`. En local lo genera Kustomize (`secretGenerator`) desde
  `overlays/local/secret.env`, ignorado por git; se crea copiando `secret.env.example`. Claves: `DB_PASSWORD`,
  `TOMTOM_API_KEY`, `API_TOKENS`. Tras cambiar un valor: `kubectl rollout restart deployment/geotracker-api`.
- **Ingress** con la estructura inicial (`08-ingress.yaml`, Traefik de k3d); en local se prueba con
  `port-forward` para evitar lidiar con dominios.
- API (`Deployment` + `Service` + `PodDisruptionBudget` + spread), PgBouncer, ETL (`CronJob`), migraciones (`Job`).
- Cambios de código 1 y 2 (modo `--una-vez` y `migrate.py` separado).
- Red OSM con el `Job` `geotracker-osm-loader` (D29, D30): descarga el extracto `.osm.pbf` del estado y carga
  la red. Para actualizarla: `kubectl delete job geotracker-osm-loader -n geotracker` y `apply -k` de nuevo.
- **ETL activo en el cluster desde 2026-11-13 17:15 UTC** (D29). El `etl_worker` de Compose está detrás del perfil
  `etl-compose`; nunca correr los dos a la vez (misma clave TomTom, pools `consumo_api` separados).

**Criterio de aceptación:** el cluster se recrea desde cero con los comandos documentados; los componentes pasan
a `Running`; el `Job` de migración queda `Completed`; el `CronJob` ejecuta ciclos; la API responde una ruta real
por `port-forward`. Borrar un pod de la API a mano y verlo recrearse solo (prueba de auto-reparación).

### Fase 2 — Robustez y nube (Azure)

**Entregable:** `overlays/nube` sobre AKS, con:

- **BD con CloudNativePG** (opción B): intercambiar `03-database-statefulset.yaml` por `03b-database-cnpg.yaml`
  en el `kustomization`, tras instalar el operador y **validar la compatibilidad de la imagen fijada**.
  Repuntar `DATABASE_READ_URL` a `geotracker-db-ro` y `DATABASE_WRITE_URL` a `geotracker-db-rw`.
- **Secrets por Sealed Secrets o External Secrets**: permite versionar una versión cifrada del secreto que solo
  el cluster descifra (en local, crear por comando basta; en la nube con GitOps, no).
- **Ingress mejorado con token**: la validación del token en la API ya existe (D26, header `X-API-Key`);
  en la nube falta TLS (sin TLS el token viaja en claro) y, opcionalmente, restricción por IP. TLS con cert-manager. Esto implementa el control de acceso "solo mis dispositivos" (D8).
- **Infraestructura con Terraform** (`deploy/terraform/`): cluster AKS, red, registro de imágenes, backend
  remoto para el `tfstate` (nunca en git, nunca con secretos en claro).
- **Prerrequisito:** actualización de TimescaleDB como playbook de Ansible (respaldo → actualización →
  verificación), con copia de los datos.

**Criterio de aceptación:** `terraform apply` crea el entorno; mismo comportamiento que en local; la BD tolera
la caída de la primaria (failover); el acceso sin token es rechazado; costos medidos contra el crédito.

### Fases posteriores (del plan original, sin cambios)

| Fase | Entregable | Criterio |
|---|---|---|
| 3 | Observabilidad: Prometheus + Alertmanager + Grafana único; API instrumentada; métricas del ETL | Alertas funcionando (ciclo fallido, cuota, 403, API, disco) |
| 4 | Migración de datos de Compose al cluster (pg_dump/pg_restore con la imagen fijada) | Conteos y ruta de prueba idénticos |
| 5 | Afinación de costos en Azure y respaldos automatizados | Crédito controlado; respaldos probados |

## Dualidad local / nube

- **Local:** k3d sobre Docker. Imágenes al cluster con `k3d image import` (no hay registro remoto).
- **Nube:** AKS. Registro: Azure Container Registry o GHCR. Imágenes con `docker push`.
- **Manifiestos:** un solo juego con **Kustomize** (`base` + `overlays/local` + `overlays/nube`). El overlay
  parcha solo lo que difiere (réplicas, almacenamiento, Ingress, secretos).

### Imágenes: de dónde sale cada una

Los manifiestos solo nombran imágenes; este es el contexto de construcción de cada una (verificado contra el
repo y `docker-compose.yml` el 2026-11-12):

| Imagen en los manifiestos | Contexto / Dockerfile | Usada por | Nombre que le da Compose |
|---|---|---|---|
| `geotracker-api:latest` | `./services/api` (`services/api/Dockerfile`, copia `app/`) | `05-api.yaml` | `geotracker-api` (igual) |
| `geotracker-etl:latest` | `.` (raíz, `Dockerfile`; copia `src/`, `data/`, `db/migrations/`) | `06-etl-cronjob.yaml`, `07-migrations-job.yaml` | `geotracker-etl_worker` (**distinto**: hay que re-etiquetar) |
| `geotracker-timescaledb:pg15.2-ts2.10.2` | `./db` (`db/Dockerfile`, base fijada por digest) | `03` / `03b` | igual (D17) |
| `bitnamilegacy/pgbouncer:1.22.1` | Docker Hub | `04-pgbouncer.yaml` | — |

Las rutas de los comandos (`python src/main.py`, `python src/migrate.py`) resuelven contra `WORKDIR /app` de la
imagen del ETL; `migrate.py` busca `/app/db/migrations`, que la imagen incluye.

```bash
docker build -t geotracker-api:latest ./services/api
docker build -t geotracker-etl:latest .            # o: docker tag geotracker-etl_worker:latest geotracker-etl:latest
k3d image import geotracker-api:latest geotracker-etl:latest geotracker-timescaledb:pg15.2-ts2.10.2 -c geotracker
```
⚠️ No reconstruir `geotracker-timescaledb` para esto: ya existe (D17); solo importarla.

## Herramientas complementarias

Decidido (D18): **Terraform y Ansible se usarán** (fase 2+); **Prometheus** para observabilidad (fase 3);
**Puppet descartado**. CI/CD (Jenkins o GitHub Actions) sigue abierto (fase 3+).

Cada herramienta tiene **un solo dominio** que gestiona. El choque no viene de usar varias, sino de que dos
administren lo mismo (cada una "corrige" el estado de la otra: *drift*).

| Capa | Herramienta | Gestiona | **No** debe gestionar |
|---|---|---|---|
| Infraestructura | **Terraform** (u OpenTofu) | Cluster, red, nodos, volúmenes, registro, BD gestionada (si fuera opción C) | Lo que corre dentro del cluster |
| Configuración de máquinas | **Ansible** | Preparar hosts/VMs, actualización de TimescaleDB como playbook | Las apps; lo que hace Terraform |
| Aplicaciones | **Kustomize** (+ opcional Argo CD) | Deployments, CronJob, Services, Secrets/ConfigMaps | La infraestructura subyacente |
| Construcción y pruebas | **CI** (Jenkins o GitHub Actions) | Construir imágenes, tests, publicar en el registro | Desplegar si se usa GitOps: un solo camino |
| Observabilidad | **Prometheus** + Alertmanager + Grafana | Métricas de operación y alertas | — |

### Terraform

- **Para qué:** en la nube, el cluster AKS, la red/VNet, el registro de imágenes, los buckets para respaldos.
  En local es opcional (k3d ya es reproducible con un comando).
- **Dualidad:** módulos por componente y un `tfvars` por entorno, en paralelo a los overlays de Kustomize.
- **Estado (`tfstate`):** contiene datos sensibles. En local, archivo fuera de git; en la nube, backend remoto
  con bloqueo. Agregar `*.tfstate*` y `.terraform/` a `.gitignore`.
- **Secretos:** nunca en `.tf` ni `tfvars` versionados; usar `TF_VAR_*` o el gestor de secretos de Azure.
- **Límite con Kubernetes:** Terraform hasta el cluster y componentes de plataforma (ingress controller,
  stack de Prometheus); las apps de geoTracker con Kustomize.

### Ansible

- **Para qué:** preparar hosts si se usaran VMs; **la actualización de TimescaleDB como playbook repetible**
  (respaldo → actualización → verificación) — prerrequisito de la fase 2.
- **Sin agentes** (solo SSH). Con AKS 100 % administrado, su uso principal es el playbook de actualización y la
  opción C (descartada), así que su peso es menor.
- **Secretos:** Ansible Vault o variables de entorno; nada en claro en el repo.

### Prometheus (fase 3) — decidido en D31 y D32

- **Instalación:** chart `kube-prometheus-stack` por Helm con `values` versionados en `deploy/k8s/platform/`.
- **Grafana único: el del chart**, con dos datasources (TimescaleDB para negocio con el rol `grafana_ro`,
  Prometheus para operación) y dashboards como ConfigMaps. Sin Pushgateway.
- **Alertas:** un solo juego de reglas de Grafana provisionadas. Notifica Grafana Alerting en local y el
  Alertmanager del chart en la nube (D31).
- **Qué medir:**

  | Componente | Métricas | Cómo |
  |---|---|---|
  | ETL | Ciclo exitoso/fallido, duración, aristas medidas, tiles | `kube-state-metrics` (último éxito del `CronJob`, **disponible**) + tabla `ejecuciones_etl` en la BD (pendiente) |
  | Cuotas TomTom | Consumo del mes vs cuota, 403 | Ya en `consumo_api`: regla de Grafana sobre SQL |
  | API | Latencia, errores, peticiones | **Hecho (D32, D33):** `/metrics` con `prometheus-fastapi-instrumentator` + `ServiceMonitor` |
  | BD | Conexiones, tamaño, locks, chunks/compresión | `postgres_exporter` |
  | Cluster | CPU/memoria de pods, reinicios, PVCs | Incluido en el chart |

- **Alertas iniciales — todas hechas (D40, D41)** en `platform/grafana/alerts/` (`etl`, `tomtom`, `api`, `bd`.yaml),
  más "ETL sin datos de tiles" sobre `ejecuciones_etl`. El plan original: sin ciclo exitoso del ETL en 45 min; consumo de cuota por encima del ritmo diario;
  cualquier 403 de TomTom; API no sana o p95 de `/rutas` > 2 s; PVC de la BD > 80 %.
- **Costo:** el stack pide ~1–2 GB de RAM; en local, configuración reducida (sin réplicas, retención corta).
- **Retención:** corta (7–15 días) para operación; el histórico de tráfico sigue en TimescaleDB.

### Observabilidad: operación (D33)

Orden en un cluster nuevo: **primero el chart** (crea el namespace `monitoring` y los CRDs como `ServiceMonitor`),
**después** `apply -k`. Mientras no exista `grafana-secret`, el pod de Grafana espera en `CreateContainerConfigError`
y arranca solo en cuanto el `apply -k` lo crea.

```bash
# 0. Secretos (una vez): cp deploy/k8s/overlays/local/grafana.env.example deploy/k8s/overlays/local/grafana.env y llenar
# 1. Chart (Helm desde un contenedor; repetir para aplicar cambios en los values)
docker run --rm --network host -v ~/.kube:/root/.kube:ro -v "$PWD":/apps -w /apps alpine/helm:3.17.3 \
  upgrade --install kps oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version 93.3.0 -n monitoring --create-namespace \
  -f deploy/k8s/platform/kube-prometheus-stack/values.yaml \
  -f deploy/k8s/platform/kube-prometheus-stack/values-local.yaml
# 2. App + dashboards + ServiceMonitor
kubectl apply -k deploy/k8s/overlays/local
# 3. Grafana del cluster. Se dejó en 3001 porque el 3000 era del Grafana de Compose (grafana_bi, apagado en D35):
#    OTRA instancia con otras credenciales. Aquí: GF_SECURITY_ADMIN_* de grafana.env.
#    3001 = puerto local; 80 = puerto del Service, que reenvía al 3000 del pod.
kubectl port-forward -n monitoring svc/kps-grafana 3001:80          # http://localhost:3001
kubectl port-forward -n monitoring svc/kps-prometheus 9090:9090     # Prometheus (opcional)
```

- **Agregar o cambiar un dashboard:** edítalo en Grafana, exporta el JSON (Export → sin "for sharing externally") a
  `deploy/k8s/platform/grafana/dashboards/`, agrégalo a `configMapGenerator` en `platform/kustomization.yaml` y
  `apply -k`. Los dashboards provisionados no se guardan desde la UI: la fuente es git. Las consultas deben apuntar al
  datasource `timescaledb` o `prometheus` (uids fijos).
- **Contraseña de `grafana_ro`:** cambiarla en `grafana.env`, `apply -k`, volver a correr el Job de migraciones
  (`kubectl delete job geotracker-migrate -n geotracker` + `apply -k`) y `kubectl rollout restart
  deployment/kps-grafana -n monitoring`.
- **IPs fijas de los nodos k3d (D34):** al reiniciar (`k3d cluster stop/start` o reinicio de Docker/WSL), Docker
  reasignaba las IPs de la red `k3d-geotracker` según el orden de arranque. El 2026-11-16 `agent-1` quedó en
  172.23.0.3 mientras su objeto `Node` decía 172.23.0.5 (ahora del `serverlb`): k3s se apagaba en bucle con
  `failed to find interface with specified node ip` y `k3d cluster start` fallaba al editar `/etc/hosts`
  ("container is restarting"). Arreglo aplicado: IPs estáticas por contenedor, que Docker conserva entre reinicios.
  Si se recrea el cluster, o vuelve a pasar:
  ```bash
  k3d cluster stop geotracker
  for p in server-0:172.23.0.2 serverlb:172.23.0.3 agent-0:172.23.0.4 agent-1:172.23.0.5; do
    docker network disconnect k3d-geotracker k3d-geotracker-${p%%:*}
    docker network connect --ip ${p##*:} k3d-geotracker k3d-geotracker-${p%%:*}
  done
  k3d cluster start geotracker
  ```
  Usar las IPs que muestre `kubectl get nodes -o wide` (las del registro de cada `Node`); al `serverlb` darle una
  libre. Diagnóstico: `docker logs --tail 30 k3d-geotracker-agent-N` y comparar con
  `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <contenedor>`.
- **Al reactivar el cluster** el ETL retoma solo en el siguiente `*/15`, pero lo no medido mientras estuvo apagado se
  pierde (2026-11-14 08:00 → 2026-11-16 07:46 UTC). La alerta "ETL sin correr" (D40) lo marca.
- **Alertas (D40, D41):** reglas en `deploy/k8s/platform/grafana/alerts/`, un archivo por dominio, todas en el
  ConfigMap `grafana-alertas` (`grafana_alert=1`, cargado por el sidecar de alertas; en la UI son de solo lectura).
  Agregar una regla: nuevo archivo o regla con `uid` único → listarlo en `platform/kustomization.yaml` → `apply -k`. Estado en Grafana → Alerting → Alert rules (carpeta geoTracker).
  **Con el equipo apagado a diario:** al encender, el último éxito tiene horas → "ETL sin correr" dispara ~2 min
  después de que arranca Grafana y se resuelve sola tras el primer ciclo (siguiente `*/15`). Si no se resuelve en
  ~20 min, el `CronJob` se saltó el ciclo (p. ej. al reanudar de una suspensión fuera de `startingDeadlineSeconds`)
  o el Job falla: `kubectl get cronjob,jobs -n geotracker`. **Sin punto de contacto configurado** las alertas solo
  se ven en Grafana (el de por defecto es un correo sin SMTP).
- **Memoria de la BD (D39):** los parámetros de memoria van como `args` del StatefulSet (sobrescriben el
  `postgresql.conf` del volumen). Si se cambia `limits.memory`, ajustarlos juntos.
- **Imágenes a k3d:** importar **una por comando**. Con dos en el mismo `k3d image import` se quedó la versión
  anterior en los nodos aunque reportó éxito (D33).

### CI/CD (abierto, fase 3+)

- **Qué hace el pipeline:** construir imágenes (ETL, API) con tag = commit, correr los tests (los de
  integración con una BD efímera con la imagen fijada), validar manifiestos y `terraform plan`, publicar.
- **GitHub Actions:** sin servidor que mantener; el repo ya está en GitHub.
- **Jenkins:** tiene sentido si se busca aprenderlo; requiere mantenimiento y ~1 GB RAM.
- **Un solo camino de despliegue:** o el CI aplica los manifiestos, o el CI solo publica y Argo CD/Flux despliega.

### Puppet: descartado

Se traslapa con el modelo de contenedores; la preparación de hosts la cubre Ansible sin agentes; exige servidor
y agentes permanentes y chocaría con Kubernetes sobre los nodos.

## Migración de datos

Los datos acumulados en Compose se trasladan con `pg_dump -Fc` y `pg_restore` en la BD del cluster, usando **la
misma imagen fijada** para no mezclar versiones de extensiones. Verificar después: conteos por tabla, versiones
de extensiones, `schema_migrations` y una ruta de prueba. Durante el corte, detener el ETL en Compose para no
partir los datos entre dos BD. (Fase 4.)

## Riesgos

- **Cuota TomTom duplicada** si por error corren dos ETL (Compose y cluster, o dos pods): `concurrencyPolicy:
  Forbid` y apagar el ETL de Compose al migrar. El pool (`consumo_api`) limita el daño solo si ambos usan la
  misma BD.
- **Términos de TomTom** sobre almacenar datos derivados de tiles (D8): free tier permite uso comercial;
  pendiente verificar la cláusula de almacenamiento/redistribución de *tiles* antes de exponer la API
  públicamente. Mitigado en el diseño: la API no es pública, se restringe por token a dispositivos propios.
- **Compatibilidad de la imagen con CloudNativePG** (fase 2): validar antes de confiar en `03b`; si la imagen
  fijada no es compatible de fábrica, construir una basada en las imágenes del operador + las extensiones.
- **Versión de la BD:** no actualizarla "de paso" al migrar a Kubernetes; es una migración propia (fase 2).
- **Costos en la nube:** el plano de control + volúmenes + balanceador cuestan; apagar el cluster cuando no se
  use y medir contra el crédito.
- **Tamaño de imágenes:** BD 4.5 GB y ETL 2.3 GB alargan despliegues; valorar multi-etapa o separar el
  `osmnx_loader` del ETL del ciclo.
- **Memoria en local:** cluster + imágenes pesadas sobre 7.6 GiB para Docker; el overlay local usa 1 réplica
  y `requests` ajustados. Si hay pods `Pending`/`Evicted`, es memoria: bajar `requests` o réplicas.

## Preguntas del milestone — estado

| # | Pregunta | Respuesta |
|---|---|---|
| 1 | ¿Local primero o ambos? | **Local primero**, nube después |
| 2 | ¿Proveedor y presupuesto? | **Azure (AKS)**, crédito gratuito de $100 USD; medir y apagar al no usar |
| 3 | BD: ¿A, B o C? ¿Actualizar TimescaleDB? | **A en local (fase 1), B en nube (fase 2)**; actualización como prerrequisito de fase 2 |
| 4 | ¿Kustomize o Helm? | **Kustomize** |
| 5 | ¿API pública o interna? | **Interna / restringida por token** a dispositivos propios; sin store |
| 6 | CI/CD y camino de despliegue | Abierto (fase 3) |
| 7 | Cluster local: ¿k3d o k3s? | **k3d** sobre Docker |
| 8 | Terraform: ¿cuál y dónde el `tfstate`? | Abierto (fase 2); backend remoto en Azure, fuera de git |
| 9 | Prometheus: ¿Grafana del chart? ¿Pushgateway? | **Grafana del chart, sin Pushgateway** (D31) |
| 10 | Alertas mínimas y canal | **Reglas de Grafana; Grafana Alerting en local, Alertmanager en la nube** (D31). Canal: abierto |
