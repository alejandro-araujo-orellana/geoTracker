import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

for _var in ("TOMTOM_API_KEY", "DB_USER", "DB_PASSWORD", "DB_NAME"):
    os.environ.setdefault(_var, "test")

import main  # noqa: E402

PUNTOS = {f"p{i}": f"22.1{i},-101.0" for i in range(30)}
PICO_LOCAL = datetime(2026, 11, 9, 18, 0, tzinfo=ZoneInfo("America/Mexico_City"))


def _planificar(consumo, ultimas=None, ahora=PICO_LOCAL):
    with patch.object(main.presupuesto, "leer_consumo", return_value=consumo), \
         patch.object(main.presupuesto, "ultimas_lecturas", return_value=ultimas or {}), \
         patch.object(main.config, "COORDENADAS_ESTATICAS", PUNTOS), \
         patch.object(main.config, "TOMTOM_CUOTA_MENSUAL", 20_000):
        return main.planificar_ciclo(MagicMock(), ahora)


def test_hora_pico_con_presupuesto_completo():
    puntos, cupo = _planificar((0, 0, False))
    # 9 nov: quedan 22 días → 18 000/22 = 818 al día; pico = 818·3/148.8 ≈ 16
    assert cupo.limite == 16 and len(puntos) == 16


def test_bloqueado_hoy_no_planifica_nada():
    puntos, cupo = _planificar((0, 10, True))
    assert puntos == {} and cupo.limite == 0


def test_cuota_del_mes_agotada():
    puntos, cupo = _planificar((18_000, 0, False))
    assert puntos == {} and cupo.limite == 0


# ---------------------------------------------------------------------------
# Tiles (D8)
# ---------------------------------------------------------------------------
def _planificar_tiles(consumo):
    with patch.object(main.presupuesto, "leer_consumo", return_value=consumo) as leer:
        cupo = main.planificar_tiles(MagicMock(), PICO_LOCAL)
    assert leer.call_args.kwargs.get("api") == main.presupuesto.API_FLOW_TILES
    return cupo


def test_tiles_pasada_completa_con_presupuesto():
    assert _planificar_tiles((0, 0, False)).limite == 49


def test_tiles_sin_presupuesto_para_pasada_completa():
    assert _planificar_tiles((179_000, 0, False)).limite == 0


def test_tiles_bloqueados_hoy():
    assert _planificar_tiles((0, 49, True)).limite == 0


def _ciclo(tiles=([], 0), medidas=0, segment=None, cupo_tiles=49, puntos=None):
    """
    Ejecuta ciclo_etl con todo lo externo simulado; devuelve los mocks.
    extraer y run_extraction son async: patch los convierte en AsyncMock,
    así que return_value es lo que devuelve el await.
    """
    m = {}
    with patch.object(main, "planificar_tiles", return_value=main.presupuesto.Cupo(cupo_tiles)), \
         patch.object(main, "planificar_ciclo", return_value=(puntos or {}, main.presupuesto.Cupo(5))), \
         patch.object(main.extractor_tiles, "extraer", return_value=tiles), \
         patch.object(main.asociador, "asociar_y_guardar", return_value=medidas) as asociar, \
         patch.object(main.extractor, "run_extraction", return_value=segment), \
         patch.object(main.presupuesto, "registrar_consumo") as registrar, \
         patch.object(main.transformer, "transform", return_value=([], [])), \
         patch.object(main.loader, "load", return_value=True), \
         patch.object(main.predictor, "ejecutar") as predictor:
        main.ciclo_etl(MagicMock())
    m.update(asociar=asociar, registrar=registrar, predictor=predictor)
    return m


def test_ciclo_con_tiles_asocia_registra_y_ejecuta_predictor():
    m = _ciclo(tiles=([{"nivel": 1.0}], 49), medidas=15_000)
    m["asociar"].assert_called_once()
    ts = m["asociar"].call_args.args[2]
    assert ts.tzinfo == timezone.utc and ts.microsecond == 0
    apis = [c.kwargs.get("api", main.presupuesto.API_FLOW_SEGMENT) for c in m["registrar"].call_args_list]
    assert main.presupuesto.API_FLOW_TILES in apis
    m["predictor"].assert_called_once()


def test_ciclo_sin_datos_de_ninguna_fuente_no_ejecuta_predictor():
    m = _ciclo(cupo_tiles=0)
    m["asociar"].assert_not_called()
    m["predictor"].assert_not_called()


def test_ciclo_registra_consumo_aunque_la_extraccion_falle():
    cupo = main.presupuesto.Cupo(5)
    with patch.object(main, "planificar_tiles", return_value=main.presupuesto.Cupo(0)), \
         patch.object(main, "planificar_ciclo", return_value=({"p0": "22.1,-101"}, cupo)), \
         patch.object(main.extractor, "run_extraction", side_effect=RuntimeError("red caída")), \
         patch.object(main.presupuesto, "registrar_consumo") as registrar:
        try:
            main.ciclo_etl(MagicMock())
        except RuntimeError:
            pass
    registrar.assert_called_once()
    _, fecha, usadas, bloqueado = registrar.call_args.args
    assert isinstance(fecha, date) and usadas == 0 and bloqueado is False
