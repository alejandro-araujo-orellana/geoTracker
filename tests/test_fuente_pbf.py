import sys
from pathlib import Path

import pytest
from shapely.geometry import box

# Al final del path: scripts/config.py no debe ocultar a src/config.py.
sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))
import osmnx as ox  # noqa: E402
from osmnx import _overpass  # noqa: E402

import fuente_pbf  # noqa: E402
from fuente_pbf import cumple_filtro, leer_elementos, parsear_filtro  # noqa: E402

DRIVE = parsear_filtro(_overpass._get_network_filter("drive"))


class TestFiltro:
    def test_parsea_el_filtro_drive_real_de_osmnx(self):
        assert DRIVE[0] == ("highway", None)
        assert {clave for clave, _ in DRIVE} >= {"highway", "area", "access", "service"}

    @pytest.mark.parametrize("filtro", ['["highway"="primary"]', '["highway"~"x"]', '["a"]basura', ""])
    def test_rechaza_sintaxis_no_soportada(self, filtro):
        with pytest.raises(ValueError):
            parsear_filtro(filtro)

    @pytest.mark.parametrize("etiquetas, esperado", [
        ({"highway": "residential"}, True),
        ({"highway": "primary", "service": "x"}, True),
        ({}, False),                                          # sin highway
        ({"highway": "footway"}, False),
        ({"highway": "service"}, False),
        ({"highway": "residential", "access": "private"}, False),
        ({"highway": "residential", "motor_vehicle": "no"}, False),
        ({"highway": "residential", "area": "yes"}, False),
        ({"highway": "residential", "service": "driveway;parking"}, False),  # búsqueda, no igualdad
        ({"highway": "Residential"}, True),                   # sensible a mayúsculas, como Overpass
    ])
    def test_semantica_overpass(self, etiquetas, esperado):
        assert cumple_filtro(etiquetas, DRIVE) is esperado


def _osm_xml(tmp_path, nodos, vias) -> Path:
    lineas = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    for nid, lat, lon, tags in nodos:
        lineas.append(f'<node id="{nid}" version="1" lat="{lat}" lon="{lon}">')
        lineas += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
        lineas.append("</node>")
    for wid, refs, tags in vias:
        lineas.append(f'<way id="{wid}" version="1">')
        lineas += [f'<nd ref="{r}"/>' for r in refs]
        lineas += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
        lineas.append("</way>")
    lineas.append("</osm>")
    ruta = tmp_path / "prueba.osm"
    ruta.write_text("\n".join(lineas), encoding="utf-8")
    return ruta


class TestLeerElementos:
    POLIGONO = box(-101.0, 22.0, -100.9, 22.1)

    def test_seleccion_igual_a_overpass(self, tmp_path):
        ruta = _osm_xml(tmp_path, nodos=[
            (1, 22.05, -100.95, {"highway": "traffic_signals"}),  # dentro
            (2, 22.30, -100.95, {}),                              # fuera
            (3, 22.06, -100.96, {}),                              # dentro
            (4, 22.40, -100.50, {}),                              # fuera
            (5, 22.41, -100.51, {}),                              # fuera
        ], vias=[
            (20, [1, 2], {"highway": "residential"}),   # un nodo dentro → entra con TODOS sus nodos
            (10, [1, 3], {"highway": "footway"}),       # no pasa el filtro
            (30, [4, 5], {"highway": "primary"}),       # completamente fuera
            (40, [3, 99], {"highway": "primary"}),      # nodo 99 no está en el extracto
        ])
        elementos = leer_elementos(ruta, self.POLIGONO, DRIVE)["elements"]

        nodos = [e for e in elementos if e["type"] == "node"]
        vias = [e for e in elementos if e["type"] == "way"]
        assert [v["id"] for v in vias] == [20]
        assert vias[0]["nodes"] == [1, 2] and vias[0]["tags"] == {"highway": "residential"}
        assert [n["id"] for n in nodos] == [1, 2]            # ordenados por id, sin el 3 (solo en vías descartadas)
        assert nodos[0]["tags"] == {"highway": "traffic_signals"} and "tags" not in nodos[1]
        assert elementos.index(nodos[-1]) < elementos.index(vias[0])   # nodos antes que vías, como `out;`

    def test_desde_pbf_alimenta_a_osmnx(self, tmp_path):
        # Cuadrado de 4 calles de doble sentido dentro del bbox → 8 aristas dirigidas.
        coords = {1: (22.05, -100.95), 2: (22.05, -100.94), 3: (22.06, -100.94), 4: (22.06, -100.95)}
        ruta = _osm_xml(
            tmp_path,
            nodos=[(i, lat, lon, {}) for i, (lat, lon) in coords.items()],
            vias=[(100 + i, [i, i % 4 + 1], {"highway": "residential"}) for i in range(1, 5)],
        )
        with fuente_pbf.desde_pbf(ruta):
            grafo = ox.graph_from_bbox((-100.99, 22.01, -100.91, 22.09), network_type="drive", simplify=False)
        assert set(grafo.nodes) == {1, 2, 3, 4}
        assert len(grafo.edges) == 8
