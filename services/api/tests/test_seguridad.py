"""
Acceso por token (D26). No necesita BD: la conexión se sustituye por un doble y,
para probar que un token válido SÍ pasa, se usa una coordenada inválida (422 ≠ 401).
"""

import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import LONGITUD_MIN_TOKEN, Settings, get_settings
from app.core.database import get_conexion
from app.main import app

TOKEN = os.environ["API_TOKENS"].split(",")[0].strip()
RUTA = "/api/v1/rutas"
PARAMS_INVALIDOS = {"origen": "abc", "destino": "22.15,-100.98"}


@pytest.fixture()
def cliente():
    app.dependency_overrides[get_conexion] = lambda: iter([None])
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_sin_header_es_401(cliente):
    r = cliente.get(RUTA, params=PARAMS_INVALIDOS)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "ApiKey"


def test_token_incorrecto_es_401(cliente):
    r = cliente.get(RUTA, params=PARAMS_INVALIDOS, headers={"X-API-Key": "x" * 64})
    assert r.status_code == 401


def test_token_correcto_pasa_la_autenticacion(cliente):
    r = cliente.get(RUTA, params=PARAMS_INVALIDOS, headers={"X-API-Key": TOKEN})
    assert r.status_code == 422  # llegó a validar coordenadas: ya pasó el token


def test_health_no_exige_token(cliente, monkeypatch):
    monkeypatch.setattr("app.main.verificar_conexiones", lambda: {"timescaledb": "ok"})
    assert cliente.get("/health").status_code == 200


def test_cualquiera_de_varios_tokens_es_valido(cliente, monkeypatch):
    otro = "otro-dispositivo-" + "b" * 32
    monkeypatch.setattr(get_settings(), "api_tokens", f"{TOKEN}, {otro}")
    assert cliente.get(RUTA, params=PARAMS_INVALIDOS, headers={"X-API-Key": otro}).status_code == 422


def test_auth_desactivada_deja_pasar_sin_header(cliente, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_auth_desactivada", True)
    assert cliente.get(RUTA, params=PARAMS_INVALIDOS).status_code == 422


# ---------------------------------------------------------------------------
# Arranque: sin tokens válidos la configuración no se construye (fail closed)
# ---------------------------------------------------------------------------
def _settings(**kw):
    return Settings(db_user="u", db_password="p", db_name="d", **kw)


@pytest.mark.parametrize("tokens", ["", " , ", "corto"])
def test_sin_tokens_validos_no_arranca(tokens):
    with pytest.raises(ValidationError):
        _settings(api_tokens=tokens)


def test_token_de_longitud_minima_es_aceptado():
    assert _settings(api_tokens="a" * LONGITUD_MIN_TOKEN).tokens == ("a" * LONGITUD_MIN_TOKEN,)


def test_desactivada_arranca_sin_tokens():
    assert _settings(api_tokens="", api_auth_desactivada=True).tokens == ()
