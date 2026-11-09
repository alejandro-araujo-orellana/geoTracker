import time
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine

# Importamos la configuración y los módulos "tontos"
import config
import asociador
import extractor
import extractor_tiles
import transformer
import loader
import migrate
import predictor
import presupuesto

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Planificación de peticiones (pools independientes por API)
# ---------------------------------------------------------------------------
def planificar_tiles(engine, ahora_local: datetime) -> presupuesto.Cupo:
    """Cupo de tiles de este ciclo: la pasada completa o nada (D8)."""
    hoy = ahora_local.date()
    usado_antes, usado_hoy, bloqueado = presupuesto.leer_consumo(engine, hoy, api=presupuesto.API_FLOW_TILES)
    if bloqueado:
        logger.warning("Tiles: TomTom respondió 403 hoy (%s); sin pasadas hasta mañana.", hoy)
        return presupuesto.Cupo(0)
    del_dia = presupuesto.presupuesto_dia(config.TOMTOM_TILES_CUOTA_MENSUAL, config.TOMTOM_MARGEN_CUOTA, usado_antes, hoy)
    n_tiles = len(extractor_tiles.tiles_de_la_zona(config.TILES_ZOOM))
    cupo = presupuesto.cupo_pasada(del_dia, usado_hoy, n_tiles)
    logger.info("Presupuesto tiles: mes %d/%d antes de hoy | hoy %d/%d | pasada %s",
                usado_antes, config.TOMTOM_TILES_CUOTA_MENSUAL, usado_hoy, del_dia,
                f"{n_tiles} tiles" if cupo else "omitida (sin presupuesto)")
    return presupuesto.Cupo(cupo)


def planificar_ciclo(engine, ahora_local: datetime) -> tuple[dict, presupuesto.Cupo]:
    """
    Flow Segment Data (fuente secundaria): qué puntos consultar en este ciclo y
    con cuántas peticiones (B.2). Devuelve (puntos, cupo); puntos vacío si no hay presupuesto.
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


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------
def ciclo_tiles(engine, ahora_local: datetime) -> int:
    """Fuente principal: pasada de vector tiles → fact_flujo_aristas. Devuelve aristas medidas."""
    cupo = planificar_tiles(engine, ahora_local)
    if cupo.limite == 0:
        return 0
    timestamp = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        tramos, _ = asyncio.run(extractor_tiles.extraer(config.TOMTOM_API_KEY, config.TILES_ZOOM, cupo))
    finally:
        presupuesto.registrar_consumo(engine, ahora_local.date(), cupo.usadas, cupo.bloqueado,
                                      api=presupuesto.API_FLOW_TILES)
    return asociador.asociar_y_guardar(engine, tramos, timestamp)


def ciclo_segment_data(engine, ahora_local: datetime) -> bool:
    """Fuente secundaria: Flow Segment Data dentro de su pool. Devuelve True si cargó datos."""
    puntos, cupo = planificar_ciclo(engine, ahora_local)
    if not puntos:
        logger.info("Segment Data: sin presupuesto para este ciclo.")
        return False

    # El consumo se registra aunque la extracción falle
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
        logger.warning("Segment Data: no se obtuvieron datos.")
        return False

    datos_estaticos, datos_dinamicos = transformer.transform(datos_crudos)
    return loader.load(datos_estaticos, datos_dinamicos, engine)


def ciclo_etl(engine):
    logger.info("=== Iniciando Ciclo ETL ===")
    ahora_local = datetime.now(ZoneInfo(config.ZONA_HORARIA))

    # 1. Fuente principal (tiles). Un fallo aquí no debe impedir la secundaria.
    try:
        aristas_medidas = ciclo_tiles(engine, ahora_local)
    except Exception:
        logger.exception("Falló la pasada de tiles; se continúa con Segment Data.")
        aristas_medidas = 0

    # 2. Fuente secundaria (Segment Data)
    hubo_segment_data = ciclo_segment_data(engine, ahora_local)

    if not aristas_medidas and not hubo_segment_data:
        logger.warning("Sin datos nuevos de ninguna fuente. Se omite el predictor.")
        return
    logger.info("=== Ciclo ETL Completado (tiles: %d aristas | segment data: %s) ===",
                aristas_medidas, "sí" if hubo_segment_data else "no")

    # 3. PREDICTOR
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
