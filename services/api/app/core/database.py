"""
Conexión a TimescaleDB. La API solo LEE: el ruteo vive en la BD
(ruta_mas_rapida, v_costos_grafo, v_tiempo_libre — migraciones 011–012).
"""

from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().db_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def get_conexion() -> Iterator[Connection]:
    """Dependencia de FastAPI: una conexión por petición."""
    with get_engine().connect() as conn:
        yield conn


def verificar_conexiones() -> dict[str, str]:
    """Estado de la BD para /health."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"timescaledb": "ok"}
    except Exception as e:  # noqa: BLE001 — /health nunca debe lanzar
        return {"timescaledb": f"error: {type(e).__name__}"}
