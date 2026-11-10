import asyncio
import math

import mapbox_vector_tile
import pytest

import extractor_tiles as et
from presupuesto import Cupo


# ---------------------------------------------------------------------------
# Geometría de tiles
# ---------------------------------------------------------------------------
def test_tiles_de_la_zona_zoom_14():
    # Medido en la prueba (commit c60a55d): 49 tiles cubren la zona a zoom 14.
    assert len(et.tiles_de_la_zona(14)) == 49
    assert len(et.tiles_de_la_zona(13)) == 16


def test_a_lonlat_esquinas_del_tile():
    z, x, y = 14, *et.tile_de(22.15, -100.97, 14)
    lon0, lat0 = et.a_lonlat(z, x, y, 0, 0, 4096)          # esquina noroeste
    lon1, lat1 = et.a_lonlat(z, x, y, 4096, 4096, 4096)    # esquina sureste
    assert lon0 < -100.97 < lon1
    assert lat1 < 22.15 < lat0
    assert math.isclose(lon1 - lon0, 360 / 2 ** z)


# ---------------------------------------------------------------------------
# Decodificación
# ---------------------------------------------------------------------------
def _tile_sintetico(features):
    return mapbox_vector_tile.encode(
        [{"name": "Traffic flow", "features": features}],
        default_options={"y_coord_down": True, "extents": 4096},
    )


def test_decodificar_tile_convierte_a_lonlat_y_propiedades():
    z, x, y = 14, *et.tile_de(22.15, -100.97, 14)
    contenido = _tile_sintetico([
        {"geometry": "LINESTRING (100 100, 2000 2000)",
         "properties": {"traffic_level": 0.42, "road_category": "secondary"}},
        {"geometry": "LINESTRING (10 10, 20 20)",
         "properties": {"traffic_level": 0.0, "road_closure": True}},
    ])
    tramos = et.decodificar_tile(contenido, z, x, y)

    assert len(tramos) == 2
    lento = next(t for t in tramos if not t["cerrada"])
    assert lento["nivel"] == pytest.approx(0.42)
    assert lento["wkt"].startswith("LINESTRING(")
    lon_ini, lat_ini = map(float, lento["wkt"][len("LINESTRING("):].split(",")[0].split())
    assert (lon_ini, lat_ini) == pytest.approx(et.a_lonlat(z, x, y, 100, 100, 4096), abs=1e-9)
    assert next(t for t in tramos if t["cerrada"])["nivel"] == 0.0


def test_decodificar_tile_separa_multilinestring_y_omite_sin_nivel():
    contenido = _tile_sintetico([
        {"geometry": "MULTILINESTRING ((0 0, 10 10), (20 20, 30 30))", "properties": {"traffic_level": 0.9}},
        {"geometry": "LINESTRING (0 0, 5 5)", "properties": {"road_category": "street"}},  # sin traffic_level
    ])
    tramos = et.decodificar_tile(contenido, 14, 3500, 7100)
    assert len(tramos) == 2 and all(t["nivel"] == pytest.approx(0.9) for t in tramos)


def test_decodificar_tile_sin_capa_de_trafico():
    contenido = mapbox_vector_tile.encode([{"name": "otra", "features": []}])
    assert et.decodificar_tile(contenido, 14, 0, 0) == []


# ---------------------------------------------------------------------------
# Descarga con cupo
# ---------------------------------------------------------------------------
class _Respuesta:
    def __init__(self, status, cuerpo=b""):
        self.status, self.cuerpo = status, cuerpo

    async def read(self):
        return self.cuerpo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Sesion:
    def __init__(self, status):
        self.status, self.peticiones = status, 0

    def get(self, url, params=None, timeout=None):
        self.peticiones += 1
        return _Respuesta(self.status, _tile_sintetico([
            {"geometry": "LINESTRING (0 0, 10 10)", "properties": {"traffic_level": 1.0}}]))


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    async def _no_dormir(_):
        return None
    monkeypatch.setattr(et.asyncio, "sleep", _no_dormir)


def _pasada(sesion, cupo, tiles):
    return asyncio.run(et.descargar_pasada(sesion, "key", 14, tiles, cupo, limite_concurrencia=4))


def test_pasada_consume_un_cupo_por_tile():
    sesion, cupo = _Sesion(200), Cupo(10)
    tramos, ok = _pasada(sesion, cupo, [(1, 1), (1, 2), (2, 1)])
    assert ok == 3 and len(tramos) == 3 and cupo.usadas == 3


def test_pasada_sin_cupo_no_pide_nada():
    sesion = _Sesion(200)
    tramos, ok = _pasada(sesion, Cupo(0), [(1, 1)])
    assert (tramos, ok, sesion.peticiones) == ([], 0, 0)


def test_403_bloquea_la_pasada():
    sesion, cupo = _Sesion(403), Cupo(10)
    tramos, ok = _pasada(sesion, cupo, [(1, 1), (1, 2), (2, 1)])
    assert ok == 0 and cupo.bloqueado
    # tras el primer 403 ningún otro tile se pide
    assert sesion.peticiones == 1 and cupo.usadas == 1
