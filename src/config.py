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

# ---------------------------------------------------------------------------
# Calles monitoreadas  →  identificador : coordenadas TomTom
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
RUTA_JSON = BASE_DIR / "data" / "vialidades_maestras.json"

try:
	with open(RUTA_JSON, "r", encoding="utf-8") as f:
		COORDENADAS_ESTATICAS = json.load(f)
except FileNotFoundError:
	COORDENADAS_ESTATICAS = {}
	print(f"[WARNING] No se encontro el archivo de vias maestras en {RUTA_JSON}")
	
# Horas de interés analítico (ARCHITECTURE.md §3 — economía de peticiones)
HORAS_PICO: set[int] = {7, 8, 9, 13, 14, 17, 18, 19, 20}
