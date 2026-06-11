import logging
from shapely.geometry import LineString
from shapely.ops import transform
from pyproj import Transformer

logger = logging.getLogger(__name__)

# Definimos los transformadores una sola vez en memoria
# EPSG:4326 es el estándar GPS (Grados Lat/Lon)
# EPSG:32614 es el sistema UTM para San Luis Potosí (Metros planos)
_transformer_a_metros = Transformer.from_crs("EPSG:4326", "EPSG:32614", always_xy=True)
_transformer_a_grados = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True)

def interpolar_vias(datos_osm: dict, distancia_metros: int = 1000) -> dict:
    """
    Toma la geometría cruda de OSM y calcula puntos equidistantes.
    
    Returns:
        dict: { "Nombre_Via_Punto_1": "lat,lon", ... }
    """
    if not datos_osm or "elements" not in datos_osm:
        logger.warning("No hay datos válidos para procesar.")
        return {}

    coordenadas_maestras = {}
    
    for elemento in datos_osm["elements"]:
        if elemento["type"] != "way":
            continue
            
        tags = elemento.get("tags", {})
        nombre_via = tags.get("name")
        
        if not nombre_via:
            continue # Omitimos vías sin nombre formal
            
        # Extraemos la geometría cruda. Nota: Shapely requiere (Longitud, Latitud)
        geometria_cruda = elemento.get("geometry", [])
        if len(geometria_cruda) < 2:
            continue
            
        nodos_lon_lat = [(nodo["lon"], nodo["lat"]) for nodo in geometria_cruda]
        
        try:
            # 1. Crear la línea curva en grados
            linea_grados = LineString(nodos_lon_lat)
            
            # 2. Proyectarla a un plano en metros (C++)
            linea_metros = transform(_transformer_a_metros.transform, linea_grados)
            longitud_total = linea_metros.length
            
            # 3. Muestrear puntos cada X metros
            distancia_actual = 0
            contador_punto = 1
            
            while distancia_actual < longitud_total:
                # Extraemos el punto exacto sobre la línea
                punto_metro = linea_metros.interpolate(distancia_actual)
                
                # Lo regresamos a coordenadas GPS (Lat/Lon)
                lon, lat = _transformer_a_grados.transform(punto_metro.x, punto_metro.y)
                
                # Generamos el ID único (ej. "Salvador Nava_1")
                id_unico = f"{nombre_via.replace(' ', '_')}_{contador_punto}"
                coordenadas_maestras[id_unico] = f"{lat:.6f},{lon:.6f}"
                
                distancia_actual += distancia_metros
                contador_punto += 1
                
        except Exception as e:
            logger.error(f"Error procesando la vía '{nombre_via}': {e}")
            continue

    logger.info(f"Interpolación completa. {len(coordenadas_maestras)} puntos estratégicos generados.")
    return coordenadas_maestras
