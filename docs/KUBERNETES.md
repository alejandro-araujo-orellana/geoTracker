# Kubernetes — análisis y plan para el siguiente milestone

> Estado: **análisis previo, nada implementado** (2026-11-09). Este documento es el punto de partida para la
> sesión en la que se abra la rama del milestone de Kubernetes. Las decisiones marcadas como **abiertas** se
> toman en esa sesión y se registran en `docs/DECISIONES.md`.

## Objetivo

Desplegar geoTracker (BD, ETL, API, Grafana) sobre Kubernetes, **una vez acumulados varios días de datos** con
el stack actual de Compose, con **dualidad local / nube**: los mismos manifiestos deben poder correr en un
cluster local (desarrollo) y en uno administrado en la nube (producción).

## Conclusión del análisis

**Es posible y factible.** El proyecto ya cumple la mayoría de los requisitos de una aplicación para
Kubernetes gracias a decisiones de la rama `feat/ruteo-dijkstra-postgis`:

| Requisito de Kubernetes | Estado actual | Dónde |
|---|---|---|
| Imágenes reproducibles con versión fija | ✅ | D10, D17 |
| Contenedores inmutables (sin montar el código) | ✅ | D17 |
| Configuración por variables de entorno | ✅ (`.env` → pasará a `Secret`/`ConfigMap`) | D17 |
| Esquema versionado y aplicable de forma idempotente | ✅ `db/migrations` + `migrate.py` | D2 |
| Procesos idempotentes (repetir un ciclo no duplica) | ✅ | D4, P6 |
| Estado fuera del proceso (cuotas en la BD, no en memoria) | ✅ `consumo_api` | D7 |
| Sonda de salud de la API | ✅ `/health` | D16 |
| Logs a stdout | ✅ | — |

**La parte difícil es la base de datos** (sección propia abajo). **El costo real es operativo**: para la escala
actual (una ciudad, ciclo ETL de ~12 s cada 15 min, API ligera) Compose es suficiente; Kubernetes se justifica
por aprendizaje/portafolio, crecimiento (varias ciudades, más tráfico en la API) o alta disponibilidad.

## Línea base medida (Compose, 2026-11-09)

Datos para dimensionar `requests`/`limits` y volúmenes. Volver a medir con varios días de datos.

| Componente | Memoria en reposo | CPU en reposo | Imagen | Notas |
|---|---|---|---|---|
| BD (`trafico_db`) | ~364 MiB | ~3 % | 4.53 GB | 307 MB de datos tras 1 día con tiles |
| ETL (`etl_trafico`) | ~153 MiB | ~0 % (picos en el ciclo) | 2.27 GB | Ciclo ~12 s cada 15 min; predictor con 67 k calles |
| API (`geotracker_api`) | ~131 MiB | <1 % | 302 MB | Ruta en ~0.3 s (pgr_dijkstra carga el grafo por petición) |
| Grafana (`grafana_bi`) | ~125 MiB | ~1 % | 1.47 GB | — |

Crecimiento de datos estimado (a verificar): `trafico_calles` ~6.5 M filas/día y `fact_flujo_aristas`
~1.4 M filas/día antes de compresión (a los 7 días). Con 1 día: `trafico_calles` 113 MB, `fact_flujo_aristas` 13 MB.

## Mapeo propuesto de componentes

| Componente | Recurso de Kubernetes | Notas |
|---|---|---|
| API | `Deployment` (≥ 2 réplicas) + `Service` + `Ingress` | Readiness/liveness en `/health`. Candidata a `HorizontalPodAutoscaler` |
| ETL | **`CronJob`** `*/15 * * * *`, `concurrencyPolicy: Forbid`, `timeZone: America/Mexico_City` | Reemplaza el `while True` de `main.py`. `Forbid` garantiza que nunca corran dos ciclos (dos ciclos simultáneos gastarían cuota doble) |
| Migraciones | `Job` (o `initContainer` de la API) | Hoy corren al arrancar el ETL; con `CronJob` + API conviene un paso explícito previo al despliegue |
| Carga de red OSM | `Job` manual (`osmnx_loader.py`) | Necesita salida a internet y volumen para la caché de osmnx |
| Grafana | `Deployment` (1 réplica) + `PersistentVolumeClaim` | Datasource y dashboards por provisioning (ConfigMap) en lugar de configurarlos a mano |
| BD | `StatefulSet` + PVC, operador, o servicio externo | **Decisión abierta** (ver abajo) |
| `.env` | `Secret` (credenciales, `TOMTOM_API_KEY`) + `ConfigMap` (no sensible) | Valorar Sealed Secrets / External Secrets para la nube |
| Respaldos | `CronJob` con `pg_dump` a PVC u object storage | Hoy son manuales (`~/backups_geotracker`) |

## Base de datos — decisión abierta

Motor actual: imagen propia fijada `geotracker-timescaledb:pg15.2-ts2.10.2` (PostgreSQL 15.2, TimescaleDB 2.10.2,
PostGIS 3.3.2, pgRouting 4.0.1 — D17). Esta combinación **no** se ofrece tal cual en los servicios administrados
habituales (los Postgres administrados genéricos no suelen incluir TimescaleDB; los de Timescale pueden no incluir
pgRouting — verificar en su momento).

| Opción | A favor | En contra |
|---|---|---|
| A. `StatefulSet` simple con la imagen propia | Igual en local y nube; control total de extensiones | Respaldos, failover y actualizaciones son responsabilidad propia |
| B. Operador (p. ej. CloudNativePG) con imagen propia | Respaldos, réplicas y failover gestionados por el operador | Requiere imagen compatible con el operador; curva de aprendizaje; validar TimescaleDB + pgRouting |
| C. BD fuera del cluster (VM o servicio administrado) | Kubernetes solo para lo stateless; menos riesgo | Dos plataformas; en la nube, depende de encontrar TimescaleDB + PostGIS + pgRouting |

Recomendación inicial: **A en local** para aprender y validar; decidir **B o C para la nube** tras probarlo.

**Prerrequisito probable para la nube:** planear la actualización de PostgreSQL 15.2 / TimescaleDB 2.10.2 (≈ 3 años)
a versiones soportadas. Es una migración propia: respaldo, `ALTER EXTENSION timescaledb UPDATE` (y de pgRouting),
pruebas con una copia, plan de vuelta atrás. Recordar: TimescaleDB 2.10 no admite `ON CONFLICT` en chunks
comprimidos (D5/P8); versiones más nuevas sí, lo que permitiría simplificar `ANTIGUEDAD_MAXIMA` del predictor.

## Cambios de código previstos (antes o durante el milestone)

1. **Modo "un ciclo" en el ETL**: `python src/main.py --una-vez` (ejecuta `ciclo_etl` y termina, con código de salida
   distinto de 0 si falla) para el `CronJob`. El modo daemon puede quedarse para Compose.
2. **Sacar las migraciones del arranque del ETL** a un comando/`Job` explícito (`python src/migrate.py`).
3. **Grafana como código**: datasource y dashboards en archivos de provisioning versionados.
4. **Sondas**: el ETL como `CronJob` no necesita liveness; si se mantuviera como `Deployment`, haría falta un latido
   (p. ej. archivo o endpoint) porque un `while True` colgado no se detecta.
5. **Apagado ordenado**: manejar `SIGTERM` en el ETL para no cortar un ciclo a la mitad (aunque es idempotente).
6. **Rendimiento de la API con réplicas**: `pgr_dijkstra` carga el grafo completo en cada petición; con más tráfico
   en la API, evaluar `pgr_aStar`, limitar el grafo por bbox origen-destino, o caché de rutas.
7. **Pool de conexiones**: con varias réplicas de la API + ETL, revisar `max_connections` de PostgreSQL o agregar PgBouncer.

## Dualidad local / nube

- **Local:** k3d o kind (también minikube, ya usado en esta máquina). Registro de imágenes local
  (`k3d registry` / `kind load docker-image`).
- **Nube:** GKE / EKS / AKS (decisión abierta). Registro: GHCR o el del proveedor.
- **Manifiestos:** un solo juego con **Kustomize** (`base` + `overlays/local` + `overlays/nube`) o un chart de Helm.
  Estructura sugerida:

```
deploy/k8s/
  base/            # Deployments, CronJob, Services, ConfigMaps, StatefulSet de la BD
  overlays/
    local/         # réplicas 1, NodePort/port-forward, PVC local-path, imágenes del registro local
    nube/          # réplicas ≥ 2, Ingress + TLS, StorageClass del proveedor, Secrets externos
```

- **CI** (opcional en el milestone): construir y publicar las imágenes con tag = commit, correr los tests de
  integración contra una BD efímera, y validar manifiestos (`kubectl --dry-run=server` / kubeconform).

## Migración de datos

Los datos acumulados en Compose se trasladan con `pg_dump -Fc` (como los respaldos actuales) y `pg_restore` en la
BD del cluster, usando **la misma imagen fijada** para no mezclar versiones de extensiones. Verificar después:
conteos por tabla, versiones de extensiones, `schema_migrations` y una ruta de prueba. Durante el corte, detener el
ETL en Compose para no partir los datos entre dos BD.

## Plan por fases (propuesta)

| Fase | Entregable | Criterio de aceptación |
|---|---|---|
| 0 | Cambios de código 1–2 + respaldo automatizado | ETL corre con `--una-vez`; migraciones como comando aparte; tests en verde |
| 1 | Cluster local + manifiestos `base` + `overlays/local` | Los 4 componentes arriba; `CronJob` ejecuta ciclos; API responde rutas |
| 2 | Datos migrados al cluster local | Conteos y ruta de prueba idénticos a Compose |
| 3 | Observabilidad mínima | Alertas si un ciclo falla, si la cuota de tiles se acerca al límite (`consumo_api`) o si la API no está sana |
| 4 | Decisión y prueba de la BD para la nube (+ actualización de TimescaleDB si aplica) | Opción B o C validada con una copia de los datos |
| 5 | `overlays/nube` en el proveedor elegido | Mismo comportamiento que en local; costos medidos |

## Riesgos

- **Cuota TomTom duplicada** si por error corren dos ETL (Compose y cluster, o dos pods): `concurrencyPolicy: Forbid`
  y apagar el ETL de Compose al migrar. El pool (`consumo_api`) limita el daño, pero solo si ambos usan la misma BD.
- **Términos de TomTom** sobre almacenar datos derivados de tiles: sigue pendiente (D8). Más relevante si el servicio
  se vuelve público en la nube.
- **Versión de la BD** (ver arriba): no actualizarla "de paso" al migrar a Kubernetes.
- **Costos en la nube:** el plano de control administrado + volúmenes + balanceador suelen costar más que una VM
  pequeña con Compose; medirlo en la fase 5.
- **Tamaño de imágenes:** BD 4.5 GB y ETL 2.3 GB (GDAL/geopandas) alargan despliegues y arranques en nodos nuevos;
  valorar imágenes multi-etapa o separar el `osmnx_loader` (pesado) del ETL del ciclo.

## Preguntas para la sesión del milestone

1. ¿Local primero y nube después, o ambos desde el inicio?
2. ¿Qué proveedor de nube (si aplica) y qué presupuesto mensual?
3. Base de datos: ¿A, B o C? ¿Se actualiza TimescaleDB antes de la nube?
4. ¿Kustomize o Helm?
5. ¿La API será pública (Ingress + TLS + autenticación / límites de uso) o interna?
6. ¿Se agrega CI para construir imágenes y validar manifiestos en este milestone?
