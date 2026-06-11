# Reglas de Arquitectura y Desarrollo: Proyecto Predictor de Tráfico

Este documento define los estándares de infraestructura, codificación y comunicación para el sistema de ingesta y análisis de tráfico vial.

## 1. Infraestructura y Despliegue (Docker)
* **Paridad de Entornos:** El desarrollo local y la producción Linux deben utilizar estrictamente Docker. No se instalarán dependencias globales en el sistema operativo anfitrión.
* **Gestión de Variables de Entorno:** Ninguna credencial, contraseña de base de datos o API Key (TomTom) debe existir en el código fuente. Se utilizará un archivo `.env` que será consumido por `docker-compose.yml` y pasará los valores al contenedor de Python. El archivo `.env` debe estar en el `.gitignore`.
* **Motor de Base de Datos:** Se utilizará exclusivamente la imagen `timescale/timescaledb:latest-pg15`. Toda tabla que registre histórico dinámico debe convertirse en *Hypertable* antes de inyectar datos.

## 2. Estándares de Código (Python)
* **Arquitectura Modular:** Queda prohibido el "código espagueti". El flujo ETL debe mantenerse separado en módulos de responsabilidad única (`extractor`, `transformer`, `loader`, `config`).
* **Ejecución Asíncrona:** Toda comunicación de red (llamadas a la API) debe ser asíncrona utilizando `aiohttp` y `asyncio` para garantizar el máximo rendimiento sin bloquear el hilo principal.
* **Gestión de Dependencias:** Toda librería externa debe estar fijada con su versión exacta en `requirements.txt` (ej. `SQLAlchemy==2.0.23`).
* **Sistema de Logging:** El uso de `print()` está prohibido para el registro de operaciones. Se debe utilizar la librería nativa `logging` de Python configurada para emitir mensajes con nivel (INFO, WARNING, ERROR) y timestamp, permitiendo una fácil lectura de los logs del contenedor Docker.

## 3. Comunicación con la API y Manejo de Errores
* **Resiliencia (Retry Pattern):** El extractor debe implementar un mecanismo de reintento. Si la API devuelve un código HTTP `429` (Too Many Requests) o `5XX` (Error de Servidor), el script debe esperar un tiempo prudencial (ej. 5 segundos) e intentar de nuevo antes de fallar completamente.
* **Timeouts:** Toda petición HTTP asíncrona debe tener un *timeout* estricto (ej. 10 segundos). Un nodo de la API que no responde no debe congelar todo el pipeline ETL.
* **Economía de Peticiones:** Las ejecuciones del orquestador (Cronjob) deben estar restringidas a horarios de interés analítico (horas pico) para optimizar el consumo de créditos gratuitos de la API.

## 4. Tratamiento de Datos (JSON y Base de Datos)
* **Aislamiento Geométrico (JSONB):** Los arreglos de coordenadas ("LineStrings") proporcionados por la API no deben insertarse en tablas transaccionales de alto flujo. Deben transformarse a formato `JSONB` y almacenarse exclusivamente en la tabla dimensional de calles para evitar redundancia espacial.
* **Validación de Esquema:** El transformador debe asumir que la API puede cambiar o fallar. Antes de extraer `currentSpeed` o `currentTravelTime`, el script debe verificar que las llaves existan en el diccionario; de lo contrario, debe asignar valores `Null` (None en Python) para evitar excepciones fatales (`KeyError`).
* **Inserción Masiva (Bulk Insert):** El módulo loader utilizará SQLAlchemy para ejecutar inserciones masivas de diccionarios. Las inserciones iterativas (un `INSERT` por cada calle) están prohibidas por su impacto negativo en el rendimiento.
* **Manejo de Upserts:** Toda inserción en base de datos debe contemplar la cláusula `ON CONFLICT DO NOTHING` (o `DO UPDATE`) gestionada por SQLAlchemy, asegurando la idempotencia del script: ejecutar el script dos veces por error no debe duplicar la información.
