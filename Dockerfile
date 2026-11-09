# Usar una imagen oficial de Python ligera
FROM python:3.11-slim
FROM timescale/timescaledb-ha:pg15-latest

USER root
RUN apt-get update && \
    apt-get install -y postgresql-15-pgrouting && \
    rm -rf /var/lib/apt/lists/*
USER postgres

# Instalar dependencias del sistema necesarias para compilar psycopg2
RUN apt-get update && apt-get install -y libpq-dev gcc gdal-bin libgdal-dev && rm -rf /var/lib/apt/lists/*

# Establecer el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiar primero el archivo de dependencias (optimiza la caché de compilación de Docker)
COPY requirements.txt .

# Instalar las librerías de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de tu código (extractor, transformer, loader, main)
COPY src/ ./src/
COPY data/ ./data/
COPY db/migrations/ ./db/migrations/

# El comando por defecto al iniciar el contenedor (cuando main.py esté listo)
CMD ["python", "src/main.py"]
