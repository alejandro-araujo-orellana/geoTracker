import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# config.py exige estas variables al importarse; valores ficticios para pruebas sin BD.
for _var in ("TOMTOM_API_KEY", "DB_USER", "DB_PASSWORD", "DB_NAME"):
    os.environ.setdefault(_var, "test")

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

import predictor  # noqa: E402

T1 = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 1, 17, 15, tzinfo=timezone.utc)
SIN_TILES = pd.DataFrame(columns=["osm_id", "nivel_relativo", "cerrada", "timestamp_lectura"])


def _lecturas(timestamps):
    return gpd.GeoDataFrame(
        {
            "id_segmento": [f"seg_{i}" for i in range(len(timestamps))],
            "nombre_via": [f"seg_{i}" for i in range(len(timestamps))],
            "nivel_congestion": [1.5] * len(timestamps),
            "timestamp_lectura": pd.to_datetime(timestamps, utc=True),
        },
        geometry=[LineString([(-100.97, 22.15 + i / 1000), (-100.971, 22.151 + i / 1000)]) for i in range(len(timestamps))],
        crs="EPSG:4326",
    ).rename_geometry("geom")


def _calles_osm(puntos=None, ids=None):
    puntos = puntos or [(700000, 2450000), (700100, 2450100)]
    ids = ids or [10 * (i + 1) for i in range(len(puntos))]
    return gpd.GeoDataFrame(
        {"osm_id": ids, "nombre": ["x"] * len(ids), "tipo_via": ["residential"] * len(ids)},
        geometry=[Point(p) for p in puntos],
        crs="EPSG:32614",
    ).rename_geometry("centroide_m")


def _flujo(filas, ts=T2):
    return pd.DataFrame(
        [{"osm_id": o, "nivel_relativo": n, "cerrada": c, "timestamp_lectura": pd.Timestamp(ts)} for o, n, c in filas]
    )


# ---------------------------------------------------------------------------
# v2 (Segment Data) — respaldo
# ---------------------------------------------------------------------------
class TestReferenciaCiclo:
    def test_es_la_lectura_mas_reciente(self):
        assert predictor.referencia_ciclo(_lecturas([T1, T2, T1])) == T2

    def test_devuelve_datetime_python_con_zona(self):
        ref = predictor.referencia_ciclo(_lecturas([T1]))
        assert type(ref) is datetime
        assert ref.tzinfo is not None


class TestLeerLecturasRecientes:
    def test_solo_usa_lecturas_dentro_de_la_ventana(self):
        engine = MagicMock()
        ahora = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
        with patch.object(predictor, "_ahora", return_value=ahora), \
             patch.object(predictor.gpd, "read_postgis", return_value=_lecturas([T1])) as leer:
            predictor.leer_lecturas_recientes(engine)

        sql = leer.call_args.args[0]
        assert "timestamp_lectura >= %(desde)s" in sql
        assert leer.call_args.kwargs["params"] == {"desde": ahora - predictor.VENTANA_LECTURAS}


# ---------------------------------------------------------------------------
# v3 (vector tiles)
# ---------------------------------------------------------------------------
class TestCongestionDesdeNivel:
    def test_inverso_del_nivel_relativo(self):
        r = predictor.congestion_desde_nivel(np.array([1.0, 0.5, 0.25]), np.array([False] * 3))
        assert r == pytest.approx([1.0, 2.0, 4.0])

    def test_tope_y_nivel_cero(self):
        r = predictor.congestion_desde_nivel(np.array([0.05, 0.0]), np.array([False, False]))
        assert r == pytest.approx([predictor.CONGESTION_MAX] * 2)

    def test_cerrada_toma_el_tope(self):
        r = predictor.congestion_desde_nivel(np.array([0.9]), np.array([True]))
        assert r == pytest.approx([predictor.CONGESTION_MAX])


class TestInferirDesdeTiles:
    def test_medidas_directas_e_idw_para_el_resto(self):
        # 10: medida (nivel 0.5 → 2.0); 20: sin medir a 100 m de 10; 30: sin medir a 5 km
        calles = _calles_osm([(700000, 2450000), (700100, 2450000), (705000, 2450000)], [10, 20, 30])
        congestion, metodos = predictor.inferir_desde_tiles(calles, _flujo([(10, 0.5, False)]))

        assert congestion == pytest.approx([2.0, 2.0, 1.0])
        assert metodos == ["TILE", "IDW_TILE", "IDW_TILE"]

    def test_calle_cerrada_no_es_fuente_del_idw(self):
        # 10 cerrada a 10 m de 30; 20 abierta (nivel 0.5) a 100 m de 30
        calles = _calles_osm([(700000, 2450000), (700100, 2450000), (700000, 2450010)], [10, 20, 30])
        congestion, metodos = predictor.inferir_desde_tiles(
            calles, _flujo([(10, 0.0, True), (20, 0.5, False)]))

        assert congestion[0] == pytest.approx(predictor.CONGESTION_MAX)  # la cerrada conserva su valor
        assert congestion[2] == pytest.approx(2.0)                       # no la contamina la cerrada
        assert metodos == ["TILE", "TILE", "IDW_TILE"]

    def test_sin_fuentes_abiertas_todo_lo_no_medido_es_flujo_libre(self):
        calles = _calles_osm([(700000, 2450000), (700100, 2450000)], [10, 20])
        congestion, _ = predictor.inferir_desde_tiles(calles, _flujo([(10, 0.0, True)]))
        assert congestion[1] == pytest.approx(1.0)

    def test_aristas_medidas_que_ya_no_estan_activas_se_ignoran(self):
        calles = _calles_osm([(700000, 2450000)], [10])
        congestion, metodos = predictor.inferir_desde_tiles(calles, _flujo([(10, 0.5, False), (999, 0.2, False)]))
        assert len(congestion) == 1 and metodos == ["TILE"]


class TestAplicarIdw:
    def test_limites_configurables(self):
        fuentes = _calles_osm([(700000, 2450000)], [1]).assign(nivel_congestion=[8.0])
        objetivo = _calles_osm([(700010, 2450000)], [2])
        assert predictor.aplicar_idw(objetivo, fuentes, 5, 2, 600)[0] == pytest.approx(3.0)            # v2: tope 3
        assert predictor.aplicar_idw(objetivo, fuentes, 5, 2, 600, limites=(1.0, 10.0))[0] == pytest.approx(8.0)


class TestGuardarResultados:
    def test_insert_es_idempotente_y_guarda_el_metodo(self):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = [(10,), (20,)]

        predictor.guardar_resultados(engine, _calles_osm(), np.array([1.2, 1.4]), T1, metodos=["TILE", "IDW_TILE"])

        stmt, registros = conn.execute.call_args.args
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT ON CONSTRAINT uq_trafico_calles_ciclo DO NOTHING" in sql
        assert "RETURNING" in sql  # conteo exacto con inserción por lotes
        assert {r["timestamp_calculo"] for r in registros} == {T1}
        assert [r["osm_id"] for r in registros] == [10, 20]
        assert [r["metodo_inferencia"] for r in registros] == ["TILE", "IDW_TILE"]
        assert all(type(r["osm_id"]) is int and type(r["nivel_congestion_inf"]) is float for r in registros)

    def test_metodo_por_defecto(self):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = []
        predictor.guardar_resultados(engine, _calles_osm(), np.array([1.2, 1.4]), T1)
        assert {r["metodo_inferencia"] for r in conn.execute.call_args.args[1]} == {"IDW_OSM"}


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------
class TestEjecutarConTiles:
    def test_usa_la_pasada_de_tiles_y_su_timestamp(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_flujo_reciente", return_value=_flujo([(10, 0.5, False)])), \
             patch.object(predictor, "ciclo_ya_calculado", return_value=False), \
             patch.object(predictor, "leer_centroides_osm", return_value=_calles_osm()), \
             patch.object(predictor, "leer_lecturas_recientes") as v2, \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        v2.assert_not_called()
        guardar.assert_called_once()
        assert guardar.call_args.args[3] == T2
        assert guardar.call_args.kwargs["metodos"] == ["TILE", "IDW_TILE"]

    def test_pasada_ya_inferida_no_recalcula(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_flujo_reciente", return_value=_flujo([(10, 0.5, False)])), \
             patch.object(predictor, "ciclo_ya_calculado", return_value=True), \
             patch.object(predictor, "leer_centroides_osm") as centroides, \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)
        centroides.assert_not_called()
        guardar.assert_not_called()


class TestEjecutarRespaldoSegmentData:
    def test_sin_tiles_recientes_usa_segment_data(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_flujo_reciente", return_value=SIN_TILES), \
             patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([T1, T2])), \
             patch.object(predictor, "_ahora", return_value=T2), \
             patch.object(predictor, "ciclo_ya_calculado", return_value=False), \
             patch.object(predictor, "leer_centroides_osm", return_value=_calles_osm()), \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        guardar.assert_called_once()
        assert guardar.call_args.args[3] == T2

    def test_ciclo_ya_calculado_no_recalcula(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_flujo_reciente", return_value=SIN_TILES), \
             patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([T1])), \
             patch.object(predictor, "_ahora", return_value=T1), \
             patch.object(predictor, "ciclo_ya_calculado", return_value=True) as ya, \
             patch.object(predictor, "leer_centroides_osm") as centroides, \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        ya.assert_called_once_with(engine, T1)
        centroides.assert_not_called()
        guardar.assert_not_called()

    def test_lecturas_mas_viejas_que_la_compresion_no_se_infieren(self):
        engine = MagicMock()
        ahora = T1 + predictor.ANTIGUEDAD_MAXIMA + timedelta(minutes=1)
        with patch.object(predictor, "leer_flujo_reciente", return_value=SIN_TILES), \
             patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([T1])), \
             patch.object(predictor, "_ahora", return_value=ahora), \
             patch.object(predictor, "ciclo_ya_calculado") as ya, \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        ya.assert_not_called()
        guardar.assert_not_called()

    def test_sin_lecturas_no_falla_ni_escribe(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_flujo_reciente", return_value=SIN_TILES), \
             patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([])), \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        guardar.assert_not_called()
