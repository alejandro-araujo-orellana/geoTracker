"""
Rol de solo lectura para Grafana (D33). Integración contra PostgreSQL/TimescaleDB real:
crea una BD temporal con todas las migraciones y la borra al terminar. Se salta si no
hay BD alcanzable (variables DB_* del entorno).

Ojo: los roles de PostgreSQL son globales al servidor, no a la BD. El test deja el rol
`grafana_ro` creado en el servidor de pruebas (con una contraseña aleatoria), igual que lo
dejaría aplicar las migraciones ahí.
"""

import os
import secrets
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

URL = "postgresql+psycopg2://{u}:{p}@{h}:{port}/{db}"
HOST = os.environ.get("DB_HOST", "localhost")
PUERTO = os.environ.get("DB_PORT", "5432")
TS = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)


def _url(usuario, clave, bd):
    return URL.format(u=usuario, p=clave, h=HOST, port=PUERTO, db=bd)


@pytest.fixture(scope="module")
def bd():
    admin = create_engine(_url(os.environ.get("DB_USER", ""), os.environ.get("DB_PASSWORD", ""), "postgres"),
                          isolation_level="AUTOCOMMIT")
    nombre = f"test_rol_grafana_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {nombre}"))
    except OperationalError:
        pytest.skip("Sin PostgreSQL alcanzable")

    import migrate
    dueno = create_engine(_url(os.environ["DB_USER"], os.environ["DB_PASSWORD"], nombre))
    migrate.aplicar_migraciones(dueno)
    with dueno.begin() as c:
        c.execute(text("""
            INSERT INTO dim_calles_osm (osm_id, u, v, k, source, target, tipo_via, longitud_m, geom, activo)
            VALUES (1, 10, 20, 0, 10, 20, 'secondary', 300,
                    ST_GeomFromText('LINESTRING(-100.980 22.150, -100.977 22.150)', 4326), TRUE)
        """))
        # Fila en la hypertable: vive en un chunk, que es donde suelen fallar los permisos.
        c.execute(text("INSERT INTO trafico_calles (timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia)"
                       " VALUES (:ts, 1, 1.5, 'TILE')"), {"ts": TS})
    yield nombre, dueno
    dueno.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {nombre} WITH (FORCE)"))


def _como_grafana(nombre_bd, clave):
    return create_engine(_url("grafana_ro", clave, nombre_bd))


def test_grafana_ro_lee_hypertables_con_su_contrasena(bd):
    import migrate
    nombre, dueno = bd
    clave = secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, clave)

    with _como_grafana(nombre, clave).connect() as c:
        filas = c.execute(text("SELECT osm_id, nivel_congestion_inf FROM trafico_calles")).all()
    assert [tuple(f) for f in filas] == [(1, 1.5)]


def test_grafana_ro_no_puede_escribir(bd):
    import migrate
    nombre, dueno = bd
    clave = secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, clave)

    with pytest.raises(ProgrammingError, match="permission denied"):
        with _como_grafana(nombre, clave).begin() as c:
            c.execute(text("DELETE FROM consumo_api"))


def test_grafana_ro_lee_tablas_creadas_despues(bd):
    """Una migración futura (p. ej. ejecuciones_etl) no debe exigir otro GRANT a mano."""
    import migrate
    nombre, dueno = bd
    with dueno.begin() as c:
        c.execute(text("CREATE TABLE tabla_futura (x int)"))
        c.execute(text("INSERT INTO tabla_futura VALUES (7)"))
    clave = secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, clave)

    with _como_grafana(nombre, clave).connect() as c:
        assert c.execute(text("SELECT x FROM tabla_futura")).scalar_one() == 7


def test_cambiar_la_contrasena_invalida_la_anterior(bd):
    import migrate
    nombre, dueno = bd
    vieja, nueva = secrets.token_hex(16), secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, vieja)
    migrate.configurar_rol_grafana(dueno, nueva)

    with pytest.raises(OperationalError, match="password authentication failed"):
        _como_grafana(nombre, vieja).connect()
    with _como_grafana(nombre, nueva).connect() as c:
        assert c.execute(text("SELECT 1")).scalar_one() == 1


def test_consultas_de_grafana_se_cortan_por_tiempo(bd):
    """Un panel pesado no debe poder dejar una consulta corriendo indefinidamente en la BD del ETL."""
    import migrate
    nombre, dueno = bd
    clave = secrets.token_hex(16)
    migrate.configurar_rol_grafana(dueno, clave)

    with pytest.raises(OperationalError, match="statement timeout"):
        with _como_grafana(nombre, clave).connect() as c:
            c.execute(text("SELECT pg_sleep(40)"))


def test_contrasena_vacia_se_rechaza(bd):
    import migrate
    _, dueno = bd
    with pytest.raises(ValueError):
        migrate.configurar_rol_grafana(dueno, "")
