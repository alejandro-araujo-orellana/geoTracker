import json
import logging
import sys
from cliente_overpass import descargar_topologia
from procesador_espacial import interpolar_vias
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

# Zona única compartida con la red OSM (src/zona.py). src/ va al final del
# path para no ocultar los módulos de scripts/.
sys.path.append(str(BASE_DIR / "src"))
import zona  # noqa: E402

def filtrar_en_zona(coordenadas: dict) -> dict:
    """
    Overpass devuelve las vías COMPLETAS que tocan el bbox, así que la
    interpolación genera puntos fuera de la zona (p. ej. carreteras que salen
    de la ciudad). Un punto fuera de la red OSM no sirve al IDW ni al ruteo
    y gasta cuota de TomTom: se descarta.
    """
    dentro = {
        nombre: punto for nombre, punto in coordenadas.items()
        if zona.contiene(*map(float, punto.split(",")))
    }
    descartados = len(coordenadas) - len(dentro)
    if descartados:
        logger.info(f"{descartados} puntos fuera de la zona de estudio descartados.")
    return dentro


def main():
    DISTANCIA_MUESTREO_METROS = 1000 # Un punto de tráfico cada kilómetro
    ARCHIVO_SALIDA = BASE_DIR / "data" / "vialidades_maestras.json"

    logger.info("=== Iniciando Generador de Topología ===")
    
    # 1. Extraer
    datos_crudos = descargar_topologia(zona.bbox_overpass())
    
    if datos_crudos:
        # 2. Transformar (Interpolar)
        coordenadas_finales = filtrar_en_zona(
            interpolar_vias(datos_crudos, DISTANCIA_MUESTREO_METROS)
        )
        
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
