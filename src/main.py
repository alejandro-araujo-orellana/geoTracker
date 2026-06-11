import time
import asyncio
import logging
from sqlalchemy import create_engine

# Importamos la configuración y los módulos "tontos"
import config
import extractor
import transformer
import loader
import predictor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def ciclo_etl(engine):
    logger.info("=== Iniciando Ciclo ETL ===")
    
    # 1. EXTRACCIÓN (Inyección de todas las variables)
    datos_crudos = asyncio.run(
        extractor.run_extraction(
            coordenadas=config.COORDENADAS_ESTATICAS,
            api_key=config.TOMTOM_API_KEY,
            base_url=config.TOMTOM_BASE_URL,
            max_reintentos=config.TOMTOM_MAX_REINTENTOS,
            tiempo_base=config.TOMTOM_TIEMPO_BASE,
            limite_concurrencia=config.TOMTOM_LIMITE_CONCURRENCIA
        )
    )
    
    if not datos_crudos:
        logger.warning("No se obtuvieron datos. Omitiendo transformación y carga.")
        return

    # 2. TRANSFORMACIÓN
    datos_estaticos, datos_dinamicos = transformer.transform(datos_crudos)

    # 3. CARGA
    loader.load(datos_estaticos, datos_dinamicos, engine)
    logger.info("=== Ciclo ETL Completado ===")

    # 4. PREDICTOR
    predictor.ejecutar(engine_postgres)
    logger.info("==== Predictor Completado ===")

if __name__ == "__main__":
    logger.info("Iniciando Orquestador Daemon de Tráfico...")
    
    # Preparamos el motor de BD y garantizamos que las tablas existan
    engine_postgres = create_engine(config.DB_URL)
    loader.create_tables(engine_postgres)
    
    INTERVALO_SEGUNDOS = 15 * 60  # 15 minutos
    
    # Bucle infinito (El corazón del contenedor)
    while True:
        ciclo_etl(engine_postgres)
        logger.info("Durmiendo por %d minutos hasta el siguiente ciclo...", INTERVALO_SEGUNDOS / 60)
        time.sleep(INTERVALO_SEGUNDOS)
