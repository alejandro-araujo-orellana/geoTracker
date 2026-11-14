FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema para psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Un solo worker por contenedor (D32): se escala con réplicas del Deployment, no con
# procesos. Con varios workers las métricas de /metrics quedarían repartidas entre procesos
# y cada scrape de Prometheus vería solo uno. Las rutas son `def` (threadpool), así que un
# worker atiende varias peticiones a la vez mientras esperan a la BD.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
