"""
Sondas y métricas (D32). No necesitan BD: el estado de la BD se sustituye con
monkeypatch sobre verificar_conexiones, que es lo único que consultan las sondas.

- /health/live  → liveness: solo el proceso. Una BD caída NO debe reiniciar la API.
- /health/ready → readiness: proceso + BD. 503 si la BD falla, para que el Service
  saque la réplica de la rotación (las sondas solo miran el código HTTP).
- /metrics      → Prometheus, sin token; etiqueta por plantilla de ruta, sin sondas.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_conexion
from app.main import app

TOKEN = os.environ["API_TOKENS"].split(",")[0].strip()
BD_OK = {"timescaledb": "ok"}
BD_CAIDA = {"timescaledb": "error: OperationalError"}


@pytest.fixture()
def cliente():
    app.dependency_overrides[get_conexion] = lambda: iter([None])
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bd(monkeypatch, estado):
    monkeypatch.setattr("app.main.verificar_conexiones", lambda: estado)


def test_live_es_200_aunque_la_bd_este_caida(cliente, monkeypatch):
    _bd(monkeypatch, BD_CAIDA)
    assert cliente.get("/health/live").status_code == 200


def test_ready_es_503_si_la_bd_esta_caida(cliente, monkeypatch):
    _bd(monkeypatch, BD_CAIDA)
    r = cliente.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["database"] == BD_CAIDA


def test_ready_es_200_con_la_bd_ok(cliente, monkeypatch):
    _bd(monkeypatch, BD_OK)
    assert cliente.get("/health/ready").status_code == 200


def test_metrics_no_exige_token(cliente):
    r = cliente.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_etiqueta_por_plantilla_de_ruta_y_no_por_url(cliente):
    cliente.get("/api/v1/rutas", params={"origen": "abc", "destino": "1,2"}, headers={"X-API-Key": TOKEN})
    texto = cliente.get("/metrics").text
    assert 'handler="/api/v1/rutas"' in texto
    assert "origen=" not in texto  # la query string nunca llega a una etiqueta


def test_metrics_no_cuenta_las_sondas(cliente, monkeypatch):
    _bd(monkeypatch, BD_OK)
    cliente.get("/health/ready")
    cliente.get("/health/live")
    cliente.get("/api/v1/rutas", params={"origen": "abc", "destino": "1,2"}, headers={"X-API-Key": TOKEN})
    texto = cliente.get("/metrics").text
    assert 'handler="/api/v1/rutas"' in texto  # el contador existe y sí registra peticiones
    assert 'handler="/health/ready"' not in texto
    assert 'handler="/health/live"' not in texto
