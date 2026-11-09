"""
Integración del ruteo (migración 011) contra PostgreSQL + pgRouting real.
Crea una BD temporal con todas las migraciones y un grafo sintético; se salta
si no hay BD alcanzable.

Grafo (lon creciente hacia el este, lat 22.150 / 22.152):

    A(-100.990,22.150) ──directa 1000 m──▶ B(-100.980,22.150)
       │                                   ▲
       └──▶ C(-100.990,22.152) ──▶ D(-100.980,22.152) ──┘     desvío ~1450 m

Todas 'secondary' (45 km/h). La directa es más corta: sin tráfico gana.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

URL_BASE = "postgresql+psycopg2://{u}:{p}@{h}:{port}/{db}".format(
    u=os.environ.get("DB_USER", ""), p=os.environ.get("DB_PASSWORD", ""),
    h=os.environ.get("DB_HOST", "localhost"), port=os.environ.get("DB_PORT", "5432"), db="{db}",
)

A, B, C, D = 1, 2, 3, 4
NODOS = {A: (-100.990, 22.150), B: (-100.980, 22.150), C: (-100.990, 22.152), D: (-100.980, 22.152)}
# osm_id, u, v, longitud_m, oneway
ARISTAS = [
    (100, A, B, 1000, True),   # directa A→B (sentido único)
    (200, A, C, 225, True),
    (300, C, D, 1000, True),
    (400, D, B, 225, True),
]


@pytest.fixture(scope="module")
def engine():
    admin = create_engine(URL_BASE.format(db="postgres"), isolation_level="AUTOCOMMIT")
    nombre = f"test_ruteo_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {nombre}"))
    except OperationalError:
        pytest.skip("Sin PostgreSQL alcanzable")

    import migrate
    eng = create_engine(URL_BASE.format(db=nombre))
    migrate.aplicar_migraciones(eng)
    with eng.begin() as c:
        for nid, (lon, lat) in NODOS.items():
            c.execute(text("INSERT INTO dim_calles_osm_vertices_pgr (id, x, y, geom) "
                           "VALUES (:i, :x, :y, ST_SetSRID(ST_MakePoint(:x, :y), 4326))"),
                      {"i": nid, "x": lon, "y": lat})
        for osm_id, u, v, largo, oneway in ARISTAS:
            (x1, y1), (x2, y2) = NODOS[u], NODOS[v]
            c.execute(text("""
                INSERT INTO dim_calles_osm (osm_id, u, v, k, source, target, tipo_via, nombre, longitud_m,
                                            oneway, activo, geom)
                VALUES (:o, :u, :v, 0, :u, :v, 'secondary', :n, :l, :ow, TRUE,
                        ST_SetSRID(ST_MakeLine(ST_MakePoint(:x1, :y1), ST_MakePoint(:x2, :y2)), 4326))
            """), {"o": osm_id, "u": u, "v": v, "n": f"calle_{osm_id}", "l": largo, "ow": oneway,
                   "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {nombre} WITH (FORCE)"))


@pytest.fixture(autouse=True)
def sin_trafico(engine):
    """Cada test parte sin datos de tráfico."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM trafico_calles; DELETE FROM fact_flujo_aristas;"))
    yield


def _trafico(engine, congestion: dict, hace="0 minutes", cerradas=()):
    with engine.begin() as c:
        for osm_id, valor in congestion.items():
            c.execute(text("""
                INSERT INTO trafico_calles (timestamp_calculo, osm_id, nivel_congestion_inf, metodo_inferencia)
                VALUES (date_trunc('second', now()) - CAST(:hace AS interval), :o, :v, 'TILE')
            """), {"o": osm_id, "v": valor, "hace": hace})
        for osm_id in cerradas:
            c.execute(text("""
                INSERT INTO fact_flujo_aristas (timestamp_lectura, osm_id, nivel_relativo, cerrada)
                VALUES (date_trunc('second', now()) - CAST(:hace AS interval), :o, 0, TRUE)
            """), {"o": osm_id, "hace": hace})


def _ruta(engine, origen=NODOS[A], destino=NODOS[B]):
    with engine.connect() as c:
        filas = c.execute(text("SELECT * FROM ruta_mas_rapida(:la, :lo, :ld, :od)"),
                          {"la": origen[1], "lo": origen[0], "ld": destino[1], "od": destino[0]}).mappings().all()
    return [f["osm_id"] for f in filas], filas


def test_sin_trafico_gana_la_ruta_mas_corta(engine):
    ids, filas = _ruta(engine)
    assert ids == [100]
    assert filas[0]["segundos"] == pytest.approx(1000 / (45 / 3.6))  # 80 s a flujo libre


def test_con_atasco_en_la_directa_toma_el_desvio(engine):
    _trafico(engine, {100: 3.0})  # directa: 80 s × 3 = 240 s > desvío ~116 s
    ids, filas = _ruta(engine)
    assert ids == [200, 300, 400]
    assert filas[-1]["segundos_acumulados"] == pytest.approx(1450 / (45 / 3.6))


def test_via_cerrada_se_excluye_aunque_sea_mas_corta(engine):
    _trafico(engine, {}, cerradas=[100])
    ids, _ = _ruta(engine)
    assert ids == [200, 300, 400]


def test_trafico_viejo_se_ignora(engine):
    _trafico(engine, {100: 3.0}, hace="2 hours", cerradas=[100])
    ids, _ = _ruta(engine)
    assert ids == [100]


def test_respeta_el_sentido_unico(engine):
    ids, _ = _ruta(engine, origen=NODOS[B], destino=NODOS[A])  # todas las aristas son A→…→B
    assert ids == []


def test_vista_costos_usa_maxspeed_razonable_y_descarta_absurdo(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE dim_calles_osm SET maxspeed_kmh = 90 WHERE osm_id = 100"))
        c.execute(text("UPDATE dim_calles_osm SET maxspeed_kmh = 3 WHERE osm_id = 200"))
    try:
        with engine.connect() as c:
            costos = dict(c.execute(text(
                "SELECT c.osm_id, v.cost FROM v_costos_grafo v JOIN dim_calles_osm c ON c.id = v.id")).fetchall())
        assert costos[100] == pytest.approx(1000 / (90 / 3.6))  # maxspeed de OSM
        assert costos[200] == pytest.approx(225 / (45 / 3.6))   # 3 km/h es absurdo → velocidad por tipo
        assert all(v > 0 for v in costos.values())
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE dim_calles_osm SET maxspeed_kmh = NULL"))


def test_tipo_via_desconocido_no_desaparece_del_grafo(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE dim_calles_osm SET tipo_via = 'living_street' WHERE osm_id = 100"))
    try:
        with engine.connect() as c:
            costo = c.execute(text(
                "SELECT v.cost FROM v_costos_grafo v JOIN dim_calles_osm c ON c.id = v.id "
                "WHERE c.osm_id = 100")).scalar()
        assert costo == pytest.approx(1000 / (20 / 3.6))  # sigue en el grafo, con velocidad por defecto
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE dim_calles_osm SET tipo_via = 'secondary' WHERE osm_id = 100"))
