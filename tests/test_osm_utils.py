import sys
from pathlib import Path

# Al final del path: scripts/config.py no debe ocultar a src/config.py.
sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))

from osm_utils import (  # noqa: E402
    id_determinista,
    normalizar_nombre,
    normalizar_oneway,
    normalizar_tipo,
    parsear_maxspeed,
)


class TestIdDeterminista:
    def test_valor_de_referencia(self):
        # Si este algoritmo cambia, todas las aristas ya cargadas cambiarían de ID.
        import hashlib

        esperado = int.from_bytes(hashlib.blake2b(b"1_2_0", digest_size=8).digest(), "big") & 0x7FFFFFFFFFFFFFFF
        assert id_determinista(1, 2, 0) == esperado

    def test_sentido_y_key_distinguen_aristas(self):
        ids = {id_determinista(1, 2, 0), id_determinista(2, 1, 0), id_determinista(1, 2, 1)}
        assert len(ids) == 3

    def test_cabe_en_bigint_positivo(self):
        for u in range(200):
            assert 0 <= id_determinista(u, u + 1, 0) <= 2**63 - 1


class TestNormalizarNombre:
    def test_texto(self):
        assert normalizar_nombre("Avenida Universidad") == "Avenida Universidad"

    def test_lista_es_independiente_del_orden(self):
        assert normalizar_nombre(["Calle B", "Calle A"]) == "Calle A"
        assert normalizar_nombre(["Calle A", "Calle B"]) == "Calle A"

    def test_lista_ignora_nulos(self):
        assert normalizar_nombre([float("nan"), "Calle A"]) == "Calle A"

    def test_nan_y_texto_nan_son_none(self):
        assert normalizar_nombre(float("nan")) is None
        assert normalizar_nombre("NaN") is None
        assert normalizar_nombre(None) is None


class TestNormalizarTipo:
    def test_lista_toma_el_de_mayor_jerarquia_sin_importar_el_orden(self):
        # osmnx arma estas listas desde un set: su orden cambia entre procesos.
        assert normalizar_tipo(["secondary", "primary"]) == "primary"
        assert normalizar_tipo(["primary", "secondary"]) == "primary"
        assert normalizar_tipo(["living_street", "residential"]) == "residential"
        assert normalizar_tipo(["residential", "living_street"]) == "residential"

    def test_lista_con_tipos_desconocidos_es_determinista(self):
        assert normalizar_tipo(["zeta", "alfa"]) == normalizar_tipo(["alfa", "zeta"]) == "alfa"

    def test_texto(self):
        assert normalizar_tipo("residential") == "residential"


class TestParsearMaxspeed:
    def test_numero_simple(self):
        assert parsear_maxspeed("50") == 50.0

    def test_con_unidad(self):
        assert parsear_maxspeed("60 km/h") == 60.0

    def test_mph(self):
        assert parsear_maxspeed("30 mph") == 48.3

    def test_lista_toma_el_menor(self):
        assert parsear_maxspeed(["60", "40"]) == 40.0

    def test_nulos_e_invalidos(self):
        assert parsear_maxspeed(float("nan")) is None
        assert parsear_maxspeed(None) is None
        assert parsear_maxspeed("signals") is None


class TestNormalizarOneway:
    def test_bool(self):
        assert normalizar_oneway(True) is True
        assert normalizar_oneway(False) is False

    def test_lista(self):
        assert normalizar_oneway([False, True]) is True

    def test_texto(self):
        assert normalizar_oneway("yes") is True
        assert normalizar_oneway("no") is False

    def test_nan(self):
        assert normalizar_oneway(float("nan")) is None
