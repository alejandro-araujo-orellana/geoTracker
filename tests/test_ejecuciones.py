"""
Registro de cada ciclo del ETL en ejecuciones_etl (D41): ciclo_etl llena un ResumenCiclo y lo
guarda al terminar, salga bien, sin datos o con excepción. Todo lo externo va simulado.
"""
import os
from unittest.mock import MagicMock, patch

for _var in ("TOMTOM_API_KEY", "DB_USER", "DB_PASSWORD", "DB_NAME"):
    os.environ.setdefault(_var, "test")

import pytest  # noqa: E402

import ejecuciones  # noqa: E402
import main  # noqa: E402


def _ciclo(tiles=([], 0), medidas=0, cupo_tiles=49, puntos=None, segment=None,
           extraer_tiles=None, run_extraction=None, registrar=None):
    """Ejecuta ciclo_etl y devuelve (ResumenCiclo registrado, excepción o None)."""
    capturado = {}

    def _registrar(engine, resumen):
        capturado["resumen"] = resumen
        if registrar:
            registrar()

    error = None
    with patch.object(main, "planificar_tiles", return_value=main.presupuesto.Cupo(cupo_tiles)), \
         patch.object(main, "planificar_ciclo", return_value=(puntos or {}, main.presupuesto.Cupo(5))), \
         patch.object(main.extractor_tiles, "extraer", return_value=tiles, side_effect=extraer_tiles), \
         patch.object(main.asociador, "asociar_y_guardar", return_value=medidas), \
         patch.object(main.extractor, "run_extraction", return_value=segment, side_effect=run_extraction), \
         patch.object(main.presupuesto, "registrar_consumo"), \
         patch.object(main.transformer, "transform", return_value=([], [])), \
         patch.object(main.loader, "load", return_value=True), \
         patch.object(main.predictor, "ejecutar"), \
         patch.object(main.ejecuciones, "registrar", side_effect=_registrar):
        try:
            main.ciclo_etl(MagicMock())
        except Exception as exc:  # noqa: BLE001 — el test inspecciona la excepción
            error = exc
    return capturado.get("resumen"), error


def test_ciclo_ok_registra_aristas_y_predictor():
    r, error = _ciclo(tiles=([{"nivel": 1.0}], 49), medidas=15_000)
    assert error is None
    assert r.estado == "ok"
    assert r.aristas_medidas == 15_000
    assert r.predictor is True
    assert r.sd_cargado is False          # sin puntos: Segment Data no corrió
    assert r.error is None
    assert r.inicio.tzinfo is not None and r.fin >= r.inicio


def test_ciclo_sin_datos_registra_sin_datos():
    r, error = _ciclo(cupo_tiles=0)
    assert error is None
    assert r.estado == "sin_datos"
    assert r.predictor is False
    assert r.aristas_medidas == 0


def test_excepcion_se_registra_y_se_propaga():
    r, error = _ciclo(cupo_tiles=0, puntos={"p0": "22.1,-101"}, run_extraction=RuntimeError("red caída"))
    assert isinstance(error, RuntimeError)
    assert r.estado == "error"
    assert "red caída" in r.error


def test_fallo_de_tiles_queda_en_error_aunque_el_ciclo_siga():
    """La pasada de tiles falla, Segment Data carga: el ciclo sigue (ok) pero el fallo queda anotado."""
    r, error = _ciclo(extraer_tiles=RuntimeError("tiles caídos"), puntos={"p0": "22.1,-101"},
                      segment=[{"dato": 1}])
    assert error is None
    assert r.estado == "ok"
    assert r.aristas_medidas == 0
    assert r.sd_cargado is True
    assert "tiles caídos" in r.error


def test_cupos_consumidos_quedan_en_el_resumen():
    cupo_tiles = main.presupuesto.Cupo(49)
    cupo_sd = main.presupuesto.Cupo(5)

    async def _extraer(*_a, **_k):
        for _ in range(49):
            cupo_tiles.tomar()
        cupo_tiles.bloquear()
        return [{"nivel": 1.0}], 49

    capturado = {}
    with patch.object(main, "planificar_tiles", return_value=cupo_tiles), \
         patch.object(main, "planificar_ciclo", return_value=({"p0": "22.1,-101"}, cupo_sd)), \
         patch.object(main.extractor_tiles, "extraer", side_effect=_extraer), \
         patch.object(main.asociador, "asociar_y_guardar", return_value=10), \
         patch.object(main.extractor, "run_extraction", return_value=None), \
         patch.object(main.presupuesto, "registrar_consumo"), \
         patch.object(main.predictor, "ejecutar"), \
         patch.object(main.ejecuciones, "registrar", side_effect=lambda e, r: capturado.update(r=r)):
        cupo_sd.tomar()
        main.ciclo_etl(MagicMock())
    r = capturado["r"]
    assert (r.tiles_peticiones, r.tiles_bloqueado) == (49, True)
    assert (r.sd_peticiones, r.sd_bloqueado) == (1, False)


def test_si_registrar_falla_el_ciclo_no_falla():
    """El registro es observabilidad: un fallo al guardarlo no debe tumbar el ciclo del ETL."""
    def _falla():
        raise RuntimeError("BD de registro caída")
    r, error = _ciclo(tiles=([{"nivel": 1.0}], 49), medidas=10, registrar=_falla)
    assert error is None
    assert r.estado == "ok"


def test_pod_viene_de_hostname():
    with patch.dict(os.environ, {"HOSTNAME": "geotracker-etl-123-abc"}):
        r = ejecuciones.ResumenCiclo.iniciar()
    assert r.pod == "geotracker-etl-123-abc"


@pytest.mark.parametrize("texto", ["x" * 5000])
def test_error_largo_se_recorta(texto):
    r = ejecuciones.ResumenCiclo.iniciar()
    r.anotar_error("tiles", RuntimeError(texto))
    assert len(r.error) <= ejecuciones.MAX_ERROR
