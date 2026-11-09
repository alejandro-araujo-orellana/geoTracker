import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# config.py exige estas variables al importarse; valores ficticios para pruebas sin BD.
for _var in ("TOMTOM_API_KEY", "DB_USER", "DB_PASSWORD", "DB_NAME"):
    os.environ.setdefault(_var, "test")

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402

import predictor  # noqa: E402

T1 = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 1, 17, 15, tzinfo=timezone.utc)


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


def _calles_osm():
    return gpd.GeoDataFrame(
        {"osm_id": [10, 20], "nombre": ["a", "b"], "tipo_via": ["residential"] * 2},
        geometry=[Point(700000, 2450000), Point(700100, 2450100)],
        crs="EPSG:32614",
    ).rename_geometry("centroide_m")


class TestReferenciaCiclo:
    def test_es_la_lectura_mas_reciente(self):
        assert predictor.referencia_ciclo(_lecturas([T1, T2, T1])) == T2

    def test_devuelve_datetime_python_con_zona(self):
        ref = predictor.referencia_ciclo(_lecturas([T1]))
        assert type(ref) is datetime
        assert ref.tzinfo is not None


class TestGuardarResultados:
    def test_insert_es_idempotente(self):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        conn.execute.return_value.rowcount = 2

        predictor.guardar_resultados(engine, _calles_osm(), np.array([1.2, 1.4]), T1)

        sql = str(conn.execute.call_args.args[0])
        assert "ON CONFLICT ON CONSTRAINT uq_trafico_calles_ciclo DO NOTHING" in sql
        registros = conn.execute.call_args.args[1]
        assert {r["timestamp_calculo"] for r in registros} == {T1}
        assert [r["osm_id"] for r in registros] == [10, 20]


class TestEjecutar:
    def test_usa_la_referencia_del_ciclo_como_timestamp(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([T1, T2])), \
             patch.object(predictor, "ciclo_ya_calculado", return_value=False), \
             patch.object(predictor, "leer_centroides_osm", return_value=_calles_osm()), \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        guardar.assert_called_once()
        assert guardar.call_args.args[3] == T2

    def test_ciclo_ya_calculado_no_recalcula(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([T1])), \
             patch.object(predictor, "ciclo_ya_calculado", return_value=True) as ya, \
             patch.object(predictor, "leer_centroides_osm") as centroides, \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        ya.assert_called_once_with(engine, T1)
        centroides.assert_not_called()
        guardar.assert_not_called()

    def test_sin_lecturas_no_falla_ni_escribe(self):
        engine = MagicMock()
        with patch.object(predictor, "leer_lecturas_recientes", return_value=_lecturas([])), \
             patch.object(predictor, "guardar_resultados") as guardar:
            predictor.ejecutar(engine)

        guardar.assert_not_called()
