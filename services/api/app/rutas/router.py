"""GET /api/v1/rutas — ruta más rápida con el tráfico actual."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.core.database import get_conexion
from app.rutas.esquemas import Coordenada, Ruta
from app.rutas.servicio import ErrorRuta, calcular_ruta

router = APIRouter(prefix="/rutas", tags=["rutas"])


def _coordenada(valor: str, nombre: str) -> Coordenada:
    try:
        return Coordenada.desde_texto(valor)
    except (ValueError, ValidationError):
        raise HTTPException(422, f"'{nombre}' debe ser 'lat,lon' con lat en [-90, 90] y lon en [-180, 180]")


@router.get("", response_model=Ruta, summary="Ruta más rápida con el tráfico actual")
def ruta_mas_rapida(
    origen: str = Query(..., description="'lat,lon' WGS84", examples=["22.1497,-100.9739"]),
    destino: str = Query(..., description="'lat,lon' WGS84", examples=["22.1460,-101.0130"]),
    detalle: bool = Query(False, description="Incluir el detalle por arista"),
    conn: Connection = Depends(get_conexion),
) -> Ruta:
    """
    Dijkstra sobre la red vial de SLP con el costo de cada calle = tiempo de
    recorrido con el tráfico del último ciclo (< 30 min). Si no hay tráfico
    reciente, la ruta se calcula a flujo libre y `trafico.vigente` es false.
    """
    try:
        return calcular_ruta(
            conn, _coordenada(origen, "origen"), _coordenada(destino, "destino"),
            get_settings().distancia_max_encaje_m, detalle,
        )
    except ErrorRuta as e:
        raise HTTPException(e.status, e.detalle)
