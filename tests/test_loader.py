from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from loader import (
    _COLUMNAS_DINAMICAS,
    _COLUMNAS_ESTATICAS,
    _normalizar,
    _preparar_estaticos,
    load,
)

ESTATICO = ("test", "test", [], "LINESTRING(-101.0 22.1, -101.1 22.2)")


# ---------------------------------------------------------------------------
# Pruebas unitarias: _normalizar
# ---------------------------------------------------------------------------
class TestNormalizar:
    def test_tuplas_se_convierten_a_dicts(self):
        tuplas = [("salvador_nava", "salvador_nava", [], None)]
        resultado = _normalizar(tuplas, _COLUMNAS_ESTATICAS)
        assert resultado == [
            {
                "id_segmento": "salvador_nava",
                "nombre_via": "salvador_nava",
                "coordenadas_geometria": [],
                "geom": None,
            }
        ]

    def test_dicts_pasan_sin_modificacion(self):
        dicts = [{"id_segmento": "test", "nombre_via": "test", "coordenadas_geometria": [], "geom": None}]
        assert _normalizar(dicts, _COLUMNAS_ESTATICAS) == dicts

    def test_lista_vacia_retorna_lista_vacia(self):
        assert _normalizar([], _COLUMNAS_ESTATICAS) == []

    def test_tupla_dinamica_mapea_columnas_correctamente(self):
        tupla = ("2026-01-01 07:00:00+00", "salvador_nava", 45.0, 180.0, 1.5)
        resultado = _normalizar([tupla], _COLUMNAS_DINAMICAS)
        assert resultado[0]["timestamp_lectura"] == "2026-01-01 07:00:00+00"
        assert resultado[0]["id_segmento"] == "salvador_nava"
        assert resultado[0]["nivel_congestion"] == 1.5


# ---------------------------------------------------------------------------
# Pruebas unitarias: _preparar_estaticos (P2 — geometría PostGIS)
# ---------------------------------------------------------------------------
class TestPrepararEstaticos:
    def test_wkt_se_convierte_en_st_geomfromtext(self):
        fila = _normalizar([ESTATICO], _COLUMNAS_ESTATICAS)
        geom = _preparar_estaticos(fila)[0]["geom"]
        sql = str(geom.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert sql.startswith("ST_GeomFromText(")
        assert "LINESTRING(-101.0 22.1, -101.1 22.2)" in sql
        assert "4326" in sql

    def test_sin_wkt_geom_es_none(self):
        fila = _normalizar([("x", "x", [], None)], _COLUMNAS_ESTATICAS)
        assert _preparar_estaticos(fila)[0]["geom"] is None

    def test_no_muta_la_entrada(self):
        fila = _normalizar([ESTATICO], _COLUMNAS_ESTATICAS)
        _preparar_estaticos(fila)
        assert fila[0]["geom"] == ESTATICO[3]


# ---------------------------------------------------------------------------
# Pruebas de integración: load() con DB simulada (sin tocar PostgreSQL real)
# ---------------------------------------------------------------------------
class TestLoad:
    def test_listas_vacias_retorna_true_sin_consultar_db(self):
        mock_engine = MagicMock()
        resultado = load([], [], mock_engine)
        assert resultado is True
        mock_engine.connect.assert_not_called()

    def test_error_de_db_ejecuta_rollback_y_retorna_false(self):
        mock_engine = MagicMock()

        with patch("loader.Session") as MockSession:
            mock_session = MockSession.return_value.__enter__.return_value
            mock_session.execute.side_effect = SQLAlchemyError("Fallo simulado en DB")

            resultado = load([ESTATICO], [], mock_engine)

        assert resultado is False
        mock_session.rollback.assert_called_once()
        mock_session.commit.assert_not_called()

    def test_carga_exitosa_ejecuta_commit_y_retorna_true(self):
        mock_engine = MagicMock()

        with patch("loader.Session") as MockSession:
            mock_session = MockSession.return_value.__enter__.return_value
            mock_session.execute.return_value.rowcount = 1

            resultado = load([ESTATICO], [], mock_engine)

        assert resultado is True
        mock_session.commit.assert_called_once()
        mock_session.rollback.assert_not_called()

    def test_upsert_estatico_completa_geom_solo_si_es_null(self):
        mock_engine = MagicMock()

        with patch("loader.Session") as MockSession:
            mock_session = MockSession.return_value.__enter__.return_value
            mock_session.execute.return_value.rowcount = 1
            load([ESTATICO], [], mock_engine)

        stmt = mock_session.execute.call_args_list[0].args[0]
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (id_segmento) DO UPDATE" in sql
        assert "WHERE dim_segmentos_viales.geom IS NULL" in sql
        assert "ST_GeomFromText" in sql

    def test_solo_dinamicos_tambien_funciona(self):
        mock_engine = MagicMock()

        with patch("loader.Session") as MockSession:
            mock_session = MockSession.return_value.__enter__.return_value
            mock_session.execute.return_value.rowcount = 1

            dinamico = ("2026-01-01 07:00:00+00", "salvador_nava", 45.0, 180.0, 1.5)
            resultado = load([], [dinamico], mock_engine)

        assert resultado is True
        mock_session.commit.assert_called_once()

    def test_execute_llamado_dos_veces_con_ambas_listas(self):
        mock_engine = MagicMock()

        with patch("loader.Session") as MockSession:
            mock_session = MockSession.return_value.__enter__.return_value
            mock_session.execute.return_value.rowcount = 1

            dinamico = ("2026-01-01 07:00:00+00", "test", 45.0, 180.0, 1.5)
            load([ESTATICO], [dinamico], mock_engine)

        assert mock_session.execute.call_count == 2
