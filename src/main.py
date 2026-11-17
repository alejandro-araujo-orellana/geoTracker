import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine

# Importamos la configuración y los módulos "tontos"
import config
import asociador
import ejecuciones
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
def ciclo_tiles(engine, ahora_local: datetime, resumen: ejecuciones.ResumenCiclo | None = None) -> int:
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
        if resumen:
            resumen.tiles_peticiones, resumen.tiles_bloqueado = cupo.usadas, cupo.bloqueado
    return asociador.asociar_y_guardar(engine, tramos, timestamp)


def ciclo_segment_data(engine, ahora_local: datetime, resumen: ejecuciones.ResumenCiclo | None = None) -> bool:
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
        if resumen:
            resumen.sd_peticiones, resumen.sd_bloqueado = cupo.usadas, cupo.bloqueado

    if not datos_crudos:
        logger.warning("Segment Data: no se obtuvieron datos.")
        return False

    datos_estaticos, datos_dinamicos = transformer.transform(datos_crudos)
    return loader.load(datos_estaticos, datos_dinamicos, engine)


def ciclo_etl(engine):
    """Un ciclo completo. Siempre deja su fila en ejecuciones_etl (D41), también si falla."""
    resumen = ejecuciones.ResumenCiclo.iniciar()
    try:
        _ciclo_etl(engine, resumen)
    except Exception as exc:
        resumen.estado = "error"
        resumen.anotar_error("ciclo", exc)
        raise
    finally:
        resumen.terminar()
        try:
            ejecuciones.registrar(engine, resumen)
        except Exception:
            logger.exception("No se pudo registrar el ciclo en ejecuciones_etl; el ciclo sigue.")


def _ciclo_etl(engine, resumen: ejecuciones.ResumenCiclo) -> None:
    logger.info("=== Iniciando Ciclo ETL ===")
    ahora_local = datetime.now(ZoneInfo(config.ZONA_HORARIA))

    # 1. Fuente principal (tiles). Un fallo aquí no debe impedir la secundaria.
    try:
        aristas_medidas = ciclo_tiles(engine, ahora_local, resumen)
    except Exception as exc:
        logger.exception("Falló la pasada de tiles; se continúa con Segment Data.")
        resumen.anotar_error("tiles", exc)
        aristas_medidas = 0
    resumen.aristas_medidas = aristas_medidas

    # 2. Fuente secundaria (Segment Data)
    hubo_segment_data = ciclo_segment_data(engine, ahora_local, resumen)
    resumen.sd_cargado = bool(hubo_segment_data)

    if not aristas_medidas and not hubo_segment_data:
        logger.warning("Sin datos nuevos de ninguna fuente. Se omite el predictor.")
        resumen.estado = "sin_datos"
        return
    logger.info("=== Ciclo ETL Completado (tiles: %d aristas | segment data: %s) ===",
                aristas_medidas, "sí" if hubo_segment_data else "no")

    # 3. PREDICTOR
    predictor.ejecutar(engine)
    resumen.predictor = True
    resumen.estado = "ok"
    logger.info("==== Predictor Completado ===")


def ejecutar_una_vez(engine) -> int:
    """
    Un solo ciclo y termina (CronJob de Kubernetes, D24). Código de salida 0 si el ciclo
    terminó, 1 si lanzó una excepción, para que el Job quede como fallido con su log.
    No aplica migraciones: en Kubernetes las aplica el Job geotracker-migrate, una sola
    vez, antes de la app; repetirlas cada 15 min desde el CronJob solo abriría carreras.
    """
    try:
        ciclo_etl(engine)
    except Exception:
        logger.exception("El ciclo ETL falló.")
        return 1
    return 0


def ejecutar_daemon(engine) -> None:
    """Modo Compose: aplica migraciones y repite el ciclo cada INTERVALO_MINUTOS."""
    migrate.aplicar_migraciones(engine)
    intervalo_segundos = config.INTERVALO_MINUTOS * 60
    while True:
        ciclo_etl(engine)
        logger.info("Durmiendo por %d minutos hasta el siguiente ciclo...", config.INTERVALO_MINUTOS)
        time.sleep(intervalo_segundos)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ETL de tráfico de geoTracker.")
    parser.add_argument(
        "--una-vez", action="store_true",
        help="Ejecuta un solo ciclo y termina (para el CronJob). Sin esto, corre como daemon.",
    )
    args = parser.parse_args(argv)

    engine = create_engine(config.DB_URL)
    try:
        if args.una_vez:
            logger.info("Modo un ciclo (--una-vez).")
            return ejecutar_una_vez(engine)
        logger.info("Iniciando Orquestador Daemon de Tráfico...")
        ejecutar_daemon(engine)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
