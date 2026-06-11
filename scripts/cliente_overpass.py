import requests
import logging

logger = logging.getLogger(__name__)

URL_OVERPASS = "https://overpass-api.de/api/interpreter"

def descargar_topologia(bbox: str) -> dict | None:
    """
    Descarga la geometría cruda de las vías principales desde OpenStreetMap.
    
    Args:
        bbox (str): Coordenadas en formato "lat_min,lon_min,lat_max,lon_max".
    """
    consulta_ql = f"""
    [out:json][timeout:50];
    (
      way["highway"="primary"]({bbox});
      way["highway"="trunk"]({bbox});
      way["highway"="secondary"]({bbox});
    );
    out geom;
    """
    
    headers = {"User-Agent": "PipelineTrafico_DesarrolloLocal"}

    try:
        logger.info("Solicitando topología a Overpass API...")
        respuesta = requests.post(URL_OVERPASS, data={"data": consulta_ql}, headers=headers)
        
        if respuesta.status_code == 200:
            logger.info("Topología cruda descargada con éxito.")
            return respuesta.json()
        elif respuesta.status_code == 429:
            logger.warning("Overpass API: Límite de peticiones excedido (HTTP 429).")
            return None
        else:
            logger.error(f"Error HTTP {respuesta.status_code}: {respuesta.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Error de red al contactar Overpass: {e}")
        return None
