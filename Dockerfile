# Imagen del ETL (extractor TomTom, predictor IDW, scripts de preparación).
# Una sola etapa: la base de datos tiene su propia imagen en db/Dockerfile.
FROM python:3.11.15-slim-trixie

# libpq-dev + gcc: compilar psycopg2. gdal: soporte geoespacial de geopandas/osmnx.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev gcc gdal-bin libgdal-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias primero (aprovecha la caché de capas de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
COPY db/migrations/ ./db/migrations/

CMD ["python", "src/main.py"]
