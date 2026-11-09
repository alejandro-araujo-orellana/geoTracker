import os
from dotenv import load_dotenv
from pathlib import Path
import json

load_dotenv()

# ---------------------------------------------------------------------------
# TomTom API
# ---------------------------------------------------------------------------
TOMTOM_API_KEY: str = os.environ["TOMTOM_API_KEY"]
TOMTOM_BASE_URL: str = (
    "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
)
TOMTOM_LIMITE_CONCURRENCIA: int = 20
TOMTOM_TIEMPO_BASE: float = 1.0
TOMTOM_MAX_REINTENTOS: int = 4
# ---------------------------------------------------------------------------
# Base de datos (TimescaleDB / PostgreSQL)
# ---------------------------------------------------------------------------
DB_URL: str = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ.get('DB_HOST', 'timescaledb')}:{os.environ.get('DB_PORT', '5432')}"
    f"/{os.environ['DB_NAME']}"
)

