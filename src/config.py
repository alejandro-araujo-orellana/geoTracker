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

# Pool de peticiones (src/presupuesto.py). Flow Segment Data: 20 000 gratis al MES.
# Ajustable por entorno si cambia el plan de TomTom.
TOMTOM_CUOTA_MENSUAL: int = int(os.environ.get("TOMTOM_CUOTA_MENSUAL", "20000"))
TOMTOM_MARGEN_CUOTA: float = 0.9  # se planea con el 90 % para absorber desfases de reinicio

# Traffic Flow vector tiles (fuente principal, D8): 200 000 gratis al MES.
TOMTOM_TILES_CUOTA_MENSUAL: int = int(os.environ.get("TOMTOM_TILES_CUOTA_MENSUAL", "200000"))
TILES_ZOOM: int = 14  # 49 tiles por pasada sobre la zona de estudio

# Orquestación
INTERVALO_MINUTOS: int = 15
ZONA_HORARIA: str = "America/Mexico_City"  # horas pico y "día" del presupuesto en hora local
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
