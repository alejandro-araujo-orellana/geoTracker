import json
import logging
from cliente_overpass import descargar_topologia
from procesador_espacial import interpolar_vias
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

def main():
    # Bounding Box de la zona urbana de San Luis Potosí
    # (Sur_Lat, Oeste_Lon, Norte_Lat, Este_Lon)
    BBOX_SLP = "22.08,-101.05,22.21,-100.89"
    DISTANCIA_MUESTREO_METROS = 1000 # Un punto de tráfico cada kilómetro y medio
    ARCHIVO_SALIDA = BASE_DIR / "data" / "vialidades_maestras.json"

    logger.info("=== Iniciando Generador de Topología ===")
    
    # 1. Extraer
    datos_crudos = descargar_topologia(BBOX_SLP)
    
    if datos_crudos:
        # 2. Transformar (Interpolar)
        coordenadas_finales = interpolar_vias(datos_crudos, DISTANCIA_MUESTREO_METROS)
        
        # 3. Cargar (Guardar en disco)
        if coordenadas_finales:
            with open(ARCHIVO_SALIDA, "w", encoding="utf-8") as f:
                json.dump(coordenadas_finales, f, indent=4, ensure_ascii=False)
            logger.info(f"Archivo maestro guardado exitosamente en: {ARCHIVO_SALIDA}")
        else:
            logger.error("La interpolación no generó puntos válidos.")
    else:
        logger.error("No se pudo obtener la topología de la API.")

if __name__ == "__main__":
    main()
