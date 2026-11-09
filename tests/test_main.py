import os
from datetime import date, datetime
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


def test_ciclo_registra_consumo_aunque_la_extraccion_falle():
    cupo = main.presupuesto.Cupo(5)
    with patch.object(main, "planificar_ciclo", return_value=({"p0": "22.1,-101"}, cupo)), \
         patch.object(main.extractor, "run_extraction", side_effect=RuntimeError("red caída")), \
         patch.object(main.presupuesto, "registrar_consumo") as registrar:
        try:
            main.ciclo_etl(MagicMock())
        except RuntimeError:
            pass
    registrar.assert_called_once()
    _, fecha, usadas, bloqueado = registrar.call_args.args
    assert isinstance(fecha, date) and usadas == 0 and bloqueado is False
