import asyncio

import pytest

import extractor
from presupuesto import Cupo


class _Respuesta:
    def __init__(self, status):
        self.status = status

    async def json(self):
        return {"flowSegmentData": {}}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Sesion:
    """Sesión falsa: devuelve los estados en orden y cuenta las peticiones."""

    def __init__(self, estados):
        self.estados = list(estados)
        self.peticiones = 0

    def get(self, url, timeout=None):
        self.peticiones += 1
        return _Respuesta(self.estados.pop(0) if self.estados else 200)


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    async def _no_dormir(_):
        return None
    monkeypatch.setattr(extractor.asyncio, "sleep", _no_dormir)


def _fetch(sesion, cupo, max_reintentos=4):
    return asyncio.run(extractor.fetch_traffic_data(
        sesion, "via", "22.1,-101.0", asyncio.Semaphore(5), "key", "http://x", max_reintentos, 0.0, cupo=cupo,
    ))


def test_cada_reintento_consume_cupo():
    sesion, cupo = _Sesion([503, 503, 200]), Cupo(10)
    assert _fetch(sesion, cupo) is not None
    assert sesion.peticiones == 3 and cupo.usadas == 3


def test_sin_cupo_no_hace_peticiones():
    sesion, cupo = _Sesion([200]), Cupo(0)
    assert _fetch(sesion, cupo) is None
    assert sesion.peticiones == 0


def test_cupo_se_agota_a_mitad_de_reintentos():
    sesion, cupo = _Sesion([503, 503, 503]), Cupo(2)
    assert _fetch(sesion, cupo) is None
    assert sesion.peticiones == 2


def test_403_bloquea_el_cupo():
    sesion, cupo = _Sesion([403]), Cupo(10)
    assert _fetch(sesion, cupo) is None
    assert cupo.bloqueado
    assert _fetch(_Sesion([200]), cupo) is None  # ya no se intenta nada más


def test_sin_cupo_funciona_como_antes():
    sesion = _Sesion([200])
    assert _fetch(sesion, None) is not None
