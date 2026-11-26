"""
Resumen por ciclo de las aristas medidas (continuous aggregate `resumen_trafico_ciclo`, migración
014, D38). Integración contra PostgreSQL/TimescaleDB real: BD temporal con todas las migraciones;
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
# Reciente: la política refresca solo los últimos 7 días; lo demás lo cubre la agregación en vivo.
CICLO = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=0, second=3, microsecond=0)


def _url(usuario, clave, bd):
    return URL.format(u=usuario, p=clave, h=HOST, port=PUERTO, db=bd)


@pytest.fixture(scope="module")
def bd():
    admin = create_engine(_url(os.environ.get("DB_USER", ""), os.environ.get("DB_PASSWORD", ""), "postgres"),
                          isolation_level="AUTOCOMMIT")
    nombre = f"test_resumen_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {nombre}"))
    except OperationalError:
        pytest.skip("Sin PostgreSQL alcanzable")

    import migrate
    dueno = create_engine(_url(os.environ["DB_USER"], os.environ["DB_PASSWORD"], nombre))
    migrate.aplicar_migraciones(dueno)
    with dueno.begin() as c:
        for i in range(1, 6):
            c.execute(text("""
                INSERT INTO dim_calles_osm (osm_id, u, v, k, source, target, tipo_via, longitud_m, geom, activo)
                VALUES (:i, :i * 10, :i * 10 + 1, 0, :i * 10, :i * 10 + 1, 'secondary', 100,
                        ST_GeomFromText('LINESTRING(-100.980 22.150, -100.979 22.150)', 4326), TRUE)
            """), {"i": i})
        # Ciclo: 4 aristas medidas (libre, en el umbral, congestionada, cerrada) + 1 inferida por IDW.
        filas = [(1, 1.0, "TILE"), (2, 1.3, "TILE"), (3, 2.0, "TILE"), (4, 10.0, "TILE"), (5, 5.0, "IDW_TILE")]
        for osm_id, valor, metodo in filas:
            c.execute(text("INSERT INTO trafico_calles (timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia)"
                           " VALUES (:ts, :o, :v, :m)"), {"ts": CICLO, "o": osm_id, "v": valor, "m": metodo})
    yield nombre, dueno
    dueno.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {nombre} WITH (FORCE)"))


def _resumen(conn):
    return conn.execute(text(
        "SELECT ciclo, aristas_medidas, congestion_promedio, aristas_congestionadas, aristas_cerradas"
        " FROM resumen_trafico_ciclo ORDER BY ciclo")).all()


def test_resume_solo_aristas_medidas_del_ciclo_sin_materializar(bd):
    """El último ciclo aún no está materializado: debe aparecer por la agregación en vivo."""
    _, dueno = bd
    with dueno.connect() as c:
        filas = _resumen(c)
    assert len(filas) == 1
    ciclo, medidas, promedio, congestionadas, cerradas = filas[0]
    assert ciclo == CICLO.replace(second=0)            # bucket de 15 min
    assert medidas == 4                                # la arista IDW_TILE no cuenta
    assert promedio == pytest.approx((1.0 + 1.3 + 2.0 + 10.0) / 4)
    assert congestionadas == 3                         # >= 1.3, incluye la cerrada
    assert cerradas == 1


def test_resultado_igual_tras_materializar(bd):
    nombre, dueno = bd
    with dueno.connect() as c:
        antes = _resumen(c)
    # El refresco no puede correr dentro de una transacción.
    auto = create_engine(_url(os.environ["DB_USER"], os.environ["DB_PASSWORD"], nombre), isolation_level="AUTOCOMMIT")
    with auto.connect() as c:
        c.execute(text("CALL refresh_continuous_aggregate('resumen_trafico_ciclo', NULL, NULL)"))
    auto.dispose()
    with dueno.connect() as c:
        assert _resumen(c) == antes


def test_grafana_ro_lee_el_resumen(bd):
    import migrate
    nombre, dueno = bd
    clave = secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, clave)
    with create_engine(_url("grafana_ro", clave, nombre)).connect() as c:
        assert len(_resumen(c)) == 1


def test_politica_de_refresco_registrada(bd):
    _, dueno = bd
    with dueno.connect() as c:
        config = c.execute(text(
            "SELECT config FROM timescaledb_information.jobs"
            " WHERE proc_name = 'policy_refresh_continuous_aggregate'")).scalar_one()
    assert config["start_offset"] == "7 days"
    assert config["end_offset"] == "00:15:00"
