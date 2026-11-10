"""
Cálculo de rutas: encaje de coordenadas, llamada a ruta_mas_rapida() y armado
de la respuesta. Toda la lógica de costos vive en la BD (D14, D16); aquí solo
se valida, se agrega y se da formato.
"""

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.rutas.esquemas import Coordenada, EstadoTrafico, PuntoEncajado, Ruta, Tramo


class ErrorRuta(Exception):
    """Error de negocio con código HTTP asociado."""

    def __init__(self, status: int, detalle: str):
        super().__init__(detalle)
        self.status = status
        self.detalle = detalle


_SQL_ENCAJE = text("""
    SELECT id, ST_DistanceSphere(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) AS distancia_m
    FROM dim_calles_osm_vertices_pgr
    ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
    LIMIT 1
""")

_SQL_RUTA = text("""
    SELECT r.seq, r.osm_id, r.nombre, r.tipo_via, r.longitud_m, r.segundos,
           l.segundos_libre, ST_AsGeoJSON(r.geom) AS geojson
    FROM ruta_mas_rapida(:lat_o, :lon_o, :lat_d, :lon_d) r
    JOIN v_tiempo_libre l ON l.osm_id = r.osm_id
    ORDER BY r.seq
""")

_SQL_TRAFICO = text("""
    SELECT max(timestamp_calculo) FROM trafico_calles
    WHERE timestamp_calculo > now() - interval '30 minutes'
""")


def _encajar(conn: Connection, punto: Coordenada, rol: str, distancia_max_m: float) -> tuple[int, float]:
    fila = conn.execute(_SQL_ENCAJE, {"lat": punto.lat, "lon": punto.lon}).one_or_none()
    if fila is None:
        raise ErrorRuta(503, "La red vial no está cargada")
    if fila.distancia_m > distancia_max_m:
        raise ErrorRuta(422, f"El {rol} está a {fila.distancia_m:,.0f} m de la red vial: fuera de la zona de cobertura")
    return fila.id, fila.distancia_m


def _unir_geometria(geojsons: list[str]) -> dict:
    """Une las LineString de las aristas (consecutivas y dirigidas) sin repetir los puntos de unión."""
    coords: list[list[float]] = []
    for g in geojsons:
        puntos = json.loads(g)["coordinates"]
        if coords and puntos and coords[-1] == puntos[0]:
            puntos = puntos[1:]
        coords.extend(puntos)
    return {"type": "LineString", "coordinates": coords}


def _calles_en_orden(nombres: list[str | None]) -> list[str]:
    calles: list[str] = []
    for n in nombres:
        if n and (not calles or calles[-1] != n):
            calles.append(n)
    return calles


def calcular_ruta(conn: Connection, origen: Coordenada, destino: Coordenada,
                  distancia_max_encaje_m: float, detalle: bool = False) -> Ruta:
    nodo_o, dist_o = _encajar(conn, origen, "origen", distancia_max_encaje_m)
    nodo_d, dist_d = _encajar(conn, destino, "destino", distancia_max_encaje_m)
    if nodo_o == nodo_d:
        raise ErrorRuta(422, "Origen y destino caen en el mismo punto de la red vial")

    filas = conn.execute(_SQL_RUTA, {"lat_o": origen.lat, "lon_o": origen.lon,
                                     "lat_d": destino.lat, "lon_d": destino.lon}).all()
    if not filas:
        raise ErrorRuta(404, "No hay ruta entre origen y destino (p. ej. solo se llega a contrasentido)")

    ts_trafico = conn.execute(_SQL_TRAFICO).scalar()
    duracion = sum(f.segundos for f in filas)
    libre = sum(f.segundos_libre for f in filas)

    return Ruta(
        origen=PuntoEncajado(lat=origen.lat, lon=origen.lon, distancia_encaje_m=dist_o),
        destino=PuntoEncajado(lat=destino.lat, lon=destino.lon, distancia_encaje_m=dist_d),
        distancia_m=sum(f.longitud_m for f in filas),
        duracion_s=duracion,
        duracion_flujo_libre_s=libre,
        demora_trafico_s=max(duracion - libre, 0.0),
        trafico=EstadoTrafico(timestamp=ts_trafico, vigente=ts_trafico is not None),
        calles=_calles_en_orden([f.nombre for f in filas]),
        geometria=_unir_geometria([f.geojson for f in filas]),
        tramos=[
            Tramo(osm_id=f.osm_id, nombre=f.nombre, tipo_via=f.tipo_via, longitud_m=f.longitud_m,
                  segundos=f.segundos, segundos_flujo_libre=f.segundos_libre)
            for f in filas
        ] if detalle else None,
    )
