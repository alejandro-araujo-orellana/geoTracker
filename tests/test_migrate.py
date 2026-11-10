import pytest

from migrate import DIRECTORIO_MIGRACIONES, listar_migraciones


def test_ordena_por_version_e_ignora_nombres_invalidos(tmp_path):
    for nombre in ("010_c.sql", "002_b.sql", "001_a.sql", "notas.sql", "README.md"):
        (tmp_path / nombre).write_text("SELECT 1;")

    resultado = listar_migraciones(tmp_path)

    assert [v for v, _ in resultado] == ["001", "002", "010"]
    assert [r.name for _, r in resultado] == ["001_a.sql", "002_b.sql", "010_c.sql"]


def test_versiones_duplicadas_lanzan_error(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "001_b.sql").write_text("SELECT 1;")

    with pytest.raises(ValueError, match="duplicadas"):
        listar_migraciones(tmp_path)


def test_migraciones_del_repo_son_consecutivas():
    versiones = [int(v) for v, _ in listar_migraciones(DIRECTORIO_MIGRACIONES)]
    assert versiones == list(range(1, len(versiones) + 1))
