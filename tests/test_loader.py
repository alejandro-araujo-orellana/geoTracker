from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from loader import _COLUMNAS_DINAMICAS, _COLUMNAS_ESTATICAS, _normalizar, load


# ---------------------------------------------------------------------------
# Pruebas unitarias: _normalizar
# ---------------------------------------------------------------------------
class TestNormalizar:
    def test_tuplas_se_convierten_a_dicts(self):
        tuplas = [("salvador_nava", "salvador_nava", "[]")]
        resultado = _normalizar(tuplas, _COLUMNAS_ESTATICAS)
        assert resultado == [
            {"id_segmento": "salvador_nava", "nombre_via": "salvador_nava", "geometria": "[]"}
        ]

    def test_dicts_pasan_sin_modificacion(self):
        dicts = [{"id_segmento": "test", "nombre_via": "test", "geometria": "[]"}]
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

            resultado = load([("test", "test", "[]")], [], mock_engine)

        assert resultado is False
        mock_session.rollback.assert_called_once()
        mock_session.commit.assert_not_called()

    def test_carga_exitosa_ejecuta_commit_y_retorna_true(self):
        mock_engine = MagicMock()

        with patch("loader.Session") as MockSession:
            mock_session = MockSession.return_value.__enter__.return_value
            mock_session.execute.return_value.rowcount = 1

            resultado = load([("test", "test", "[]")], [], mock_engine)

        assert resultado is True
        mock_session.commit.assert_called_once()
        mock_session.rollback.assert_not_called()

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

            estatico = ("test", "test", "[]")
            dinamico = ("2026-01-01 07:00:00+00", "test", 45.0, 180.0, 1.5)
            load([estatico], [dinamico], mock_engine)

        assert mock_session.execute.call_count == 2
