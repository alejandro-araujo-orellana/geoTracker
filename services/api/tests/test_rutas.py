"""
API de rutas contra PostgreSQL + pgRouting real (BD temporal con todas las
migraciones y un grafo sintético). Se salta si no hay BD alcanzable.

Correr desde la raíz del repo (necesita src/migrate.py y db/migrations):
    PYTHONPATH=src:services/api pytest services/api/tests

Grafo (todas 'secondary', 45 km/h, sentido único):
    A ──directa 1000 m──▶ B          A ▶ C ▶ D ▶ B   desvío 1450 m
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

for _var, _valor in (("DB_USER", "test"), ("DB_PASSWORD", "test"), ("DB_NAME", "test")):
    os.environ.setdefault(_var, _valor)

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import get_conexion  # noqa: E402
from app.main import app  # noqa: E402

URL_BASE = "postgresql+psycopg2://{u}:{p}@{h}:{port}/{db}".format(
    u=os.environ["DB_USER"], p=os.environ["DB_PASSWORD"],
    h=os.environ.get("DB_HOST", "localhost"), port=os.environ.get("DB_PORT", "5432"), db="{db}",
)
A, B, C, D = (22.150, -100.990), (22.150, -100.980), (22.152, -100.990), (22.152, -100.980)
NODOS = {1: A, 2: B, 3: C, 4: D}
ARISTAS = [(100, 1, 2, 1000), (200, 1, 3, 225), (300, 3, 4, 1000), (400, 4, 2, 225)]
LIBRE_DIRECTA = 1000 / (45 / 3.6)  # 80 s


def _coord(p):
    return f"{p[0]},{p[1]}"


@pytest.fixture(scope="module")
def engine():
    admin = create_engine(URL_BASE.format(db="postgres"), isolation_level="AUTOCOMMIT")
    nombre = f"test_api_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {nombre}"))
    except OperationalError:
        pytest.skip("Sin PostgreSQL alcanzable")

    import migrate
    eng = create_engine(URL_BASE.format(db=nombre))
    migrate.aplicar_migraciones(eng)
    with eng.begin() as c:
        for nid, (lat, lon) in NODOS.items():
            c.execute(text("INSERT INTO dim_calles_osm_vertices_pgr (id, x, y, geom) "
                           "VALUES (:i, :x, :y, ST_SetSRID(ST_MakePoint(:x, :y), 4326))"),
                      {"i": nid, "x": lon, "y": lat})
        for osm_id, u, v, largo in ARISTAS:
            (y1, x1), (y2, x2) = NODOS[u], NODOS[v]
            c.execute(text("""
                INSERT INTO dim_calles_osm (osm_id, u, v, k, source, target, tipo_via, nombre, longitud_m,
                                            oneway, activo, geom)
                VALUES (:o, :u, :v, 0, :u, :v, 'secondary', :n, :l, TRUE, TRUE,
                        ST_SetSRID(ST_MakeLine(ST_MakePoint(:x1, :y1), ST_MakePoint(:x2, :y2)), 4326))
            """), {"o": osm_id, "u": u, "v": v, "n": f"calle_{osm_id}", "l": largo,
                   "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {nombre} WITH (FORCE)"))


@pytest.fixture()
def cliente(engine):
    def _conexion():
        with engine.connect() as conn:
            yield conn

    with engine.begin() as c:
        c.execute(text("DELETE FROM trafico_calles; DELETE FROM fact_flujo_aristas;"))
    app.dependency_overrides[get_conexion] = _conexion
    yield TestClient(app)
    app.dependency_overrides.clear()


def _get(cliente, origen, destino, **extra):
    return cliente.get("/api/v1/rutas", params={"origen": origen, "destino": destino, **extra})


def test_ruta_sin_trafico(cliente):
    r = _get(cliente, _coord(A), _coord(B))
    assert r.status_code == 200, r.text
    ruta = r.json()
    assert ruta["distancia_m"] == pytest.approx(1000)
    assert ruta["duracion_s"] == pytest.approx(LIBRE_DIRECTA)
    assert ruta["duracion_flujo_libre_s"] == pytest.approx(LIBRE_DIRECTA)
    assert ruta["demora_trafico_s"] == pytest.approx(0)
    assert ruta["trafico"] == {"timestamp": None, "vigente": False}
    assert ruta["calles"] == ["calle_100"]
    assert ruta["geometria"]["type"] == "LineString"
    assert ruta["geometria"]["coordinates"] == [[A[1], A[0]], [B[1], B[0]]]
    assert ruta["tramos"] is None


def test_ruta_con_trafico_reporta_demora(cliente, engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO trafico_calles (timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia) "
                       "VALUES (date_trunc('second', now()), 100, 1.2, 'TILE')"))
    ruta = _get(cliente, _coord(A), _coord(B)).json()
    assert ruta["calles"] == ["calle_100"]                         # 96 s sigue siendo mejor que el desvío (116 s)
    assert ruta["duracion_s"] == pytest.approx(LIBRE_DIRECTA * 1.2)
    assert ruta["demora_trafico_s"] == pytest.approx(LIBRE_DIRECTA * 0.2)
    assert ruta["trafico"]["vigente"] is True and ruta["trafico"]["timestamp"] is not None


def test_desvio_une_la_geometria_en_orden(cliente, engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO trafico_calles (timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia) "
                       "VALUES (date_trunc('second', now()), 100, 3.0, 'TILE')"))
    ruta = _get(cliente, _coord(A), _coord(B), detalle="true").json()
    assert ruta["calles"] == ["calle_200", "calle_300", "calle_400"]
    assert [t["osm_id"] for t in ruta["tramos"]] == [200, 300, 400]
    coords = ruta["geometria"]["coordinates"]
    assert coords == [[A[1], A[0]], [C[1], C[0]], [D[1], D[0]], [B[1], B[0]]]  # sin puntos repetidos en las uniones


def test_sin_ruta_por_sentido_unico_es_404(cliente):
    r = _get(cliente, _coord(B), _coord(A))
    assert r.status_code == 404
    assert "ruta" in r.json()["detail"].lower()


def test_coordenada_fuera_de_la_red_es_422(cliente):
    r = _get(cliente, "22.400,-100.500", _coord(B))
    assert r.status_code == 422
    assert "origen" in r.json()["detail"]


def test_mismo_punto_es_422(cliente):
    r = _get(cliente, _coord(A), "22.15001,-100.99001")
    assert r.status_code == 422


@pytest.mark.parametrize("valor", ["abc", "22.15", "95,-100.9", "22.15,-200", "22.15,-100.9,3"])
def test_formato_de_coordenada_invalido_es_422(cliente, valor):
    assert _get(cliente, valor, _coord(B)).status_code == 422


def test_health(cliente):
    r = cliente.get("/health")
    assert r.status_code == 200
    assert "status" in r.json()
