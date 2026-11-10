"""Modelos de entrada/salida del endpoint de rutas."""

from datetime import datetime

from pydantic import BaseModel, Field


class Coordenada(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    @classmethod
    def desde_texto(cls, valor: str) -> "Coordenada":
        """'lat,lon' → Coordenada. ValueError si el formato o el rango no son válidos."""
        partes = valor.split(",")
        if len(partes) != 2:
            raise ValueError("se espera 'lat,lon'")
        return cls(lat=float(partes[0]), lon=float(partes[1]))


class PuntoEncajado(Coordenada):
    distancia_encaje_m: float = Field(description="Distancia al nodo de la red donde inicia/termina la ruta")


class Tramo(BaseModel):
    osm_id: int
    nombre: str | None
    tipo_via: str
    longitud_m: float
    segundos: float
    segundos_flujo_libre: float


class EstadoTrafico(BaseModel):
    timestamp: datetime | None = Field(description="Ciclo de tráfico usado (UTC); null si no hay ninguno reciente")
    vigente: bool = Field(description="True si la ruta usa tráfico de los últimos 30 min; si no, flujo libre")


class Ruta(BaseModel):
    origen: PuntoEncajado
    destino: PuntoEncajado
    distancia_m: float
    duracion_s: float = Field(description="Tiempo estimado con el tráfico actual")
    duracion_flujo_libre_s: float = Field(description="Tiempo de la MISMA ruta sin tráfico")
    demora_trafico_s: float = Field(description="duracion_s − duracion_flujo_libre_s")
    trafico: EstadoTrafico
    calles: list[str] = Field(description="Nombres de las calles en orden, sin repeticiones consecutivas")
    geometria: dict = Field(description="GeoJSON LineString (lon, lat) en el sentido de la ruta")
    tramos: list[Tramo] | None = Field(default=None, description="Detalle por arista (solo con detalle=true)")
