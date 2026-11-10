import sys
from pathlib import Path

# Al final del path: scripts/config.py no debe ocultar a src/config.py.
sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))

from deduplicar_puntos import deduplicar  # noqa: E402

PUNTOS = {
    "Av_A_1": "22.10,-101.00",
    "Av_A_2": "22.11,-101.00",
    "Av_A_3": "22.12,-101.00",
    "Av_B_1": "22.20,-100.95",
    "Nuevo_1": "22.15,-100.97",
}


def test_un_punto_por_segmento_conserva_el_de_mas_historia():
    firmas = {"Av_A_1": ("geomA", 10), "Av_A_2": ("geomA", 50), "Av_A_3": ("geomA", 50), "Av_B_1": ("geomB", 5)}
    resultado, descartados = deduplicar(PUNTOS, firmas)
    # empate de historia (50) → orden alfabético
    assert "Av_A_2" in resultado and "Av_A_1" not in resultado and "Av_A_3" not in resultado
    assert resultado["Av_A_2"] == PUNTOS["Av_A_2"]
    assert "Av_B_1" in resultado
    assert sorted(descartados) == ["Av_A_1", "Av_A_3"]


def test_puntos_nunca_medidos_se_conservan():
    resultado, _ = deduplicar(PUNTOS, {"Av_A_1": ("geomA", 1)})
    assert "Nuevo_1" in resultado


def test_conserva_el_orden_original():
    resultado, _ = deduplicar(PUNTOS, {})
    assert list(resultado) == list(PUNTOS)


def test_es_idempotente():
    firmas = {"Av_A_1": ("geomA", 10), "Av_A_2": ("geomA", 50), "Av_B_1": ("geomB", 5)}
    una, _ = deduplicar(PUNTOS, firmas)
    dos, descartados = deduplicar(una, firmas)
    assert una == dos and descartados == []
