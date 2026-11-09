import json
from pathlib import Path

import zona

RAIZ = Path(__file__).resolve().parent.parent


def test_bbox_overpass_es_sur_oeste_norte_este():
    assert zona.bbox_overpass() == "22.101,-101.045,22.219,-100.906"


def test_bbox_osmnx_es_oeste_sur_este_norte():
    assert zona.bbox_osmnx() == (-101.045, 22.101, -100.906, 22.219)


def test_bbox_es_coherente():
    assert zona.LAT_MIN < zona.LAT_MAX
    assert zona.LON_MIN < zona.LON_MAX


def test_contiene():
    assert zona.contiene(22.15, -100.97)
    assert not zona.contiene(22.30, -100.97)
    assert not zona.contiene(22.15, -100.80)


def test_todos_los_puntos_tomtom_estan_dentro_de_la_zona():
    # P9: medir fuera de la zona de la red OSM desperdicia peticiones a TomTom.
    puntos = json.loads((RAIZ / "data" / "vialidades_maestras.json").read_text(encoding="utf-8"))
    fuera = [k for k, v in puntos.items() if not zona.contiene(*map(float, v.split(",")))]
    assert fuera == []


def test_ningun_script_define_su_propio_bbox():
    for ruta in [*(RAIZ / "src").glob("*.py"), *(RAIZ / "scripts").glob("*.py")]:
        if ruta.name == "zona.py":
            continue
        texto = ruta.read_text(encoding="utf-8")
        assert "BBOX =" not in texto and "BBOX_SLP =" not in texto, f"{ruta.name} define un BBOX propio"
