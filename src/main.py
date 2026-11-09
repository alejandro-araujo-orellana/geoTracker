import time
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine

# Importamos la configuración y los módulos "tontos"
import config
import extractor
import transformer
import loader
import migrate
import predictor
import presupuesto

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def planificar_ciclo(engine, ahora_local: datetime) -> tuple[dict, presupuesto.Cupo]:
    """
    Decide qué puntos consultar en este ciclo y con cuántas peticiones (B.2).
    Devuelve (puntos, cupo); puntos vacío si no hay presupuesto.
    """
    hoy = ahora_local.date()
    usado_antes, usado_hoy, bloqueado = presupuesto.leer_consumo(engine, hoy)
    if bloqueado:
        logger.warning("TomTom respondió 403 hoy (%s): no se harán más peticiones hasta mañana.", hoy)
        return {}, presupuesto.Cupo(0)

    del_dia = presupuesto.presupuesto_dia(config.TOMTOM_CUOTA_MENSUAL, config.TOMTOM_MARGEN_CUOTA, usado_antes, hoy)
    restante_hoy = max(0, del_dia - usado_hoy)
    n = presupuesto.cupo_ciclo(del_dia, restante_hoy, ahora_local.hour, config.INTERVALO_MINUTOS, config.HORAS_PICO)
    puntos = presupuesto.seleccionar_puntos(config.COORDENADAS_ESTATICAS, presupuesto.ultimas_lecturas(engine), n)

    logger.info(
        "Presupuesto TomTom: mes %d/%d usadas antes de hoy | hoy %d/%d | este ciclo %d peticiones → %d de %d puntos",
        usado_antes, config.TOMTOM_CUOTA_MENSUAL, usado_hoy, del_dia, n, len(puntos), len(config.COORDENADAS_ESTATICAS),
    )
    return puntos, presupuesto.Cupo(n)


def ciclo_etl(engine):
    logger.info("=== Iniciando Ciclo ETL ===")
    ahora_local = datetime.now(ZoneInfo(config.ZONA_HORARIA))

    puntos, cupo = planificar_ciclo(engine, ahora_local)
    if not puntos:
        logger.info("Sin presupuesto para este ciclo. Se omite la extracción.")
        return

    # 1. EXTRACCIÓN — el consumo se registra aunque la extracción falle
    try:
        datos_crudos = asyncio.run(
            extractor.run_extraction(
                coordenadas=puntos,
                api_key=config.TOMTOM_API_KEY,
                base_url=config.TOMTOM_BASE_URL,
                max_reintentos=config.TOMTOM_MAX_REINTENTOS,
                tiempo_base=config.TOMTOM_TIEMPO_BASE,
                limite_concurrencia=config.TOMTOM_LIMITE_CONCURRENCIA,
                cupo=cupo,
            )
        )
    finally:
        presupuesto.registrar_consumo(engine, ahora_local.date(), cupo.usadas, cupo.bloqueado)
        logger.info("Peticiones consumidas en el ciclo: %d/%d", cupo.usadas, cupo.limite)

    if not datos_crudos:
        logger.warning("No se obtuvieron datos. Omitiendo transformación y carga.")
        return

    # 2. TRANSFORMACIÓN
    datos_estaticos, datos_dinamicos = transformer.transform(datos_crudos)

    # 3. CARGA
    loader.load(datos_estaticos, datos_dinamicos, engine)
    logger.info("=== Ciclo ETL Completado ===")

    # 4. PREDICTOR
    predictor.ejecutar(engine)
    logger.info("==== Predictor Completado ===")


if __name__ == "__main__":
    logger.info("Iniciando Orquestador Daemon de Tráfico...")

    # Preparamos el motor de BD y llevamos el esquema a la última migración
    engine_postgres = create_engine(config.DB_URL)
    migrate.aplicar_migraciones(engine_postgres)

    INTERVALO_SEGUNDOS = config.INTERVALO_MINUTOS * 60

    # Bucle infinito (El corazón del contenedor)
    while True:
        ciclo_etl(engine_postgres)
        logger.info("Durmiendo por %d minutos hasta el siguiente ciclo...", config.INTERVALO_MINUTOS)
        time.sleep(INTERVALO_SEGUNDOS)
