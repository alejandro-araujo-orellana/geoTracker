"""
ejecuciones_etl contra PostgreSQL real (migración 015, D41): BD temporal con todas las migraciones;
se salta si no hay BD alcanzable (variables DB_*).
"""
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

URL = "postgresql+psycopg2://{u}:{p}@{h}:{port}/{db}"
HOST = os.environ.get("DB_HOST", "localhost")
PUERTO = os.environ.get("DB_PORT", "5432")
INICIO = datetime(2026, 11, 17, 18, 0, 1, tzinfo=timezone.utc)


def _url(usuario, clave, bd):
    return URL.format(u=usuario, p=clave, h=HOST, port=PUERTO, db=bd)


@pytest.fixture(scope="module")
def bd():
    admin = create_engine(_url(os.environ.get("DB_USER", ""), os.environ.get("DB_PASSWORD", ""), "postgres"),
                          isolation_level="AUTOCOMMIT")
    nombre = f"test_ejecuciones_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {nombre}"))
    except OperationalError:
        pytest.skip("Sin PostgreSQL alcanzable")
    import migrate
    dueno = create_engine(_url(os.environ["DB_USER"], os.environ["DB_PASSWORD"], nombre))
    migrate.aplicar_migraciones(dueno)
    yield nombre, dueno
    dueno.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {nombre} WITH (FORCE)"))


def _resumen(**cambios):
    import ejecuciones
    r = ejecuciones.ResumenCiclo(inicio=INICIO, pod="geotracker-etl-x")
    r.fin = INICIO + timedelta(seconds=19.5)
    r.estado = "ok"
    r.aristas_medidas = 15_000
    r.tiles_peticiones = 49
    r.predictor = True
    for k, v in cambios.items():
        setattr(r, k, v)
    return r


def test_registrar_guarda_una_fila_con_duracion(bd):
    import ejecuciones
    _, dueno = bd
    ejecuciones.registrar(dueno, _resumen())
    with dueno.connect() as c:
        fila = c.execute(text("SELECT estado, aristas_medidas, tiles_peticiones, predictor, duracion_s, pod"
                              " FROM ejecuciones_etl WHERE inicio = :i"), {"i": INICIO}).one()
    assert tuple(fila) == ("ok", 15_000, 49, True, pytest.approx(19.5), "geotracker-etl-x")


def test_registrar_es_idempotente_por_inicio(bd):
    import ejecuciones
    _, dueno = bd
    ejecuciones.registrar(dueno, _resumen())
    ejecuciones.registrar(dueno, _resumen(aristas_medidas=1))
    with dueno.connect() as c:
        n, medidas = c.execute(text("SELECT count(*), max(aristas_medidas) FROM ejecuciones_etl"
                                    " WHERE inicio = :i"), {"i": INICIO}).one()
    assert (n, medidas) == (1, 15_000)


def test_estado_invalido_se_rechaza(bd):
    import ejecuciones
    _, dueno = bd
    with pytest.raises(Exception, match="estado"):
        ejecuciones.registrar(dueno, _resumen(inicio=INICIO + timedelta(hours=1), estado="raro"))


def test_grafana_ro_lee_ejecuciones(bd):
    import migrate
    nombre, dueno = bd
    clave = secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, clave)
    with create_engine(_url("grafana_ro", clave, nombre)).connect() as c:
        assert c.execute(text("SELECT count(*) FROM ejecuciones_etl")).scalar_one() >= 1
