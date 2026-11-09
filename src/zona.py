"""
zona.py
=======
Zona de estudio ÚNICA del sistema (P9 de docs/DATABASE.md).

Zona metropolitana de San Luis Potosí, incluye Soledad de Graciano Sánchez.
La usan los puntos de monitoreo TomTom (scripts/generador_topologia.py) y la
red vial OSM / grafo de ruteo (scripts/osmnx_loader.py). Si cambias estos
valores, regenera ambos: un punto TomTom fuera de la red OSM no aporta al
mapa de calor ni al ruteo y gasta cuota de la API.

Sin dependencias, para que la puedan importar src/ y scripts/.
"""

LAT_MIN: float = 22.101   # sur
LON_MIN: float = -101.045  # oeste
LAT_MAX: float = 22.219   # norte
LON_MAX: float = -100.906  # este


def bbox_overpass() -> str:
    """Formato de Overpass QL: 'sur,oeste,norte,este'."""
    return f"{LAT_MIN},{LON_MIN},{LAT_MAX},{LON_MAX}"


def bbox_osmnx() -> tuple[float, float, float, float]:
    """Formato de osmnx >= 2.0 (graph_from_bbox): (oeste, sur, este, norte)."""
    return (LON_MIN, LAT_MIN, LON_MAX, LAT_MAX)


def contiene(lat: float, lon: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX
