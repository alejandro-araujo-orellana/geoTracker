"""
Integración contra PostgreSQL/PostGIS real (la regla de asociación vive en SQL).
Crea una BD temporal con todas las migraciones y la borra al terminar.
Se salta si no hay BD alcanzable (variables DB_* del entorno).
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

URL_BASE = "postgresql+psycopg2://{u}:{p}@{h}:{port}/{db}".format(
    u=os.environ.get("DB_USER", ""), p=os.environ.get("DB_PASSWORD", ""),
    h=os.environ.get("DB_HOST", "localhost"), port=os.environ.get("DB_PORT", "5432"), db="{db}",
)
TS = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def engine():
    admin = create_engine(URL_BASE.format(db="postgres"), isolation_level="AUTOCOMMIT")
    nombre = f"test_asociador_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {nombre}"))
    except OperationalError:
        pytest.skip("Sin PostgreSQL alcanzable")

    import migrate
    eng = create_engine(URL_BASE.format(db=nombre))
    migrate.aplicar_migraciones(eng)
    # Calle de doble sentido: dos aristas dirigidas sobre la misma línea (~300 m, este-oeste)
    with eng.begin() as c:
        c.execute(text("""
            INSERT INTO dim_calles_osm (osm_id, u, v, k, source, target, tipo_via, longitud_m, geom, activo)
            VALUES
              (1, 10, 20, 0, 10, 20, 'secondary', 300,
               ST_GeomFromText('LINESTRING(-100.980 22.150, -100.977 22.150)', 4326), TRUE),
              (2, 20, 10, 0, 20, 10, 'secondary', 300,
               ST_GeomFromText('LINESTRING(-100.977 22.150, -100.980 22.150)', 4326), TRUE),
              (3, 30, 40, 0, 30, 40, 'residential', 300,
               ST_GeomFromText('LINESTRING(-100.990 22.160, -100.990 22.163)', 4326), FALSE)
        """))
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {nombre} WITH (FORCE)"))


def _tramo(wkt, nivel, cerrada=False):
    return {"nivel": nivel, "cerrada": cerrada, "categoria": "secondary", "wkt": wkt}


def _medidas(engine, ts):
    with engine.connect() as c:
        return dict(c.execute(text(
            "SELECT osm_id, nivel_relativo FROM fact_flujo_aristas WHERE timestamp_lectura = :ts"),
            {"ts": ts}).fetchall())


def test_cada_sentido_recibe_su_propio_tramo(engine):
    import asociador
    # tramos desplazados ~4 m a cada lado, uno por sentido, con niveles distintos
    hacia_este = _tramo("LINESTRING(-100.9801 22.15004, -100.9769 22.15004)", 0.4)
    hacia_oeste = _tramo("LINESTRING(-100.9769 22.14996, -100.9801 22.14996)", 0.9)

    n = asociador.asociar_y_guardar(engine, [hacia_este, hacia_oeste], TS)

    assert n == 2
    assert _medidas(engine, TS) == {1: pytest.approx(0.4), 2: pytest.approx(0.9)}


def test_tramo_de_un_solo_sentido_no_se_asigna_al_contrario(engine):
    import asociador
    ts = TS.replace(minute=15)
    n = asociador.asociar_y_guardar(engine, [_tramo("LINESTRING(-100.9801 22.15004, -100.9769 22.15004)", 0.5)], ts)
    assert n == 1 and set(_medidas(engine, ts)) == {1}


def test_tramo_lejano_o_arista_inactiva_no_se_asocian(engine):
    import asociador
    ts = TS.replace(minute=30)
    lejos = _tramo("LINESTRING(-100.9801 22.1505, -100.9769 22.1505)", 0.5)        # ~55 m
    sobre_inactiva = _tramo("LINESTRING(-100.990 22.160, -100.990 22.163)", 0.5)
    assert asociador.asociar_y_guardar(engine, [lejos, sobre_inactiva], ts) == 0


def test_es_idempotente(engine):
    import asociador
    ts = TS.replace(minute=45)
    tramos = [_tramo("LINESTRING(-100.9801 22.15004, -100.9769 22.15004)", 0.4)]
    assert asociador.asociar_y_guardar(engine, tramos, ts) == 1
    assert asociador.asociar_y_guardar(engine, tramos, ts) == 0
