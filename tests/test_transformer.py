import pytest

from transformer import _calcular_nivel_congestion, coordenadas_a_wkt, transform


# ---------------------------------------------------------------------------
# Pruebas unitarias: coordenadas_a_wkt
# ---------------------------------------------------------------------------
class TestCoordenadasAWkt:
    def test_orden_lon_lat(self):
        coords = [{"latitude": 22.1, "longitude": -101.0}, {"latitude": 22.2, "longitude": -101.1}]
        assert coordenadas_a_wkt(coords) == "LINESTRING(-101.0 22.1, -101.1 22.2)"

    def test_un_solo_punto_retorna_none(self):
        assert coordenadas_a_wkt([{"latitude": 22.1, "longitude": -101.0}]) is None

    def test_lista_vacia_o_none_retorna_none(self):
        assert coordenadas_a_wkt([]) is None
        assert coordenadas_a_wkt(None) is None

    def test_puntos_incompletos_se_descartan(self):
        coords = [
            {"latitude": 22.1, "longitude": -101.0},
            {"latitude": 22.15},
            "basura",
            {"latitude": 22.2, "longitude": -101.1},
        ]
        assert coordenadas_a_wkt(coords) == "LINESTRING(-101.0 22.1, -101.1 22.2)"

    def test_precision_completa(self):
        coords = [
            {"latitude": 22.13190841766569, "longitude": -101.03314346657906},
            {"latitude": 22.13186152118997, "longitude": -101.03304288374099},
        ]
        assert "-101.03314346657906 22.13190841766569" in coordenadas_a_wkt(coords)


# ---------------------------------------------------------------------------
# Pruebas unitarias: _calcular_nivel_congestion
# ---------------------------------------------------------------------------
class TestCalcularNivelCongestion:
    def test_calculo_normal(self):
        assert _calcular_nivel_congestion(180, 120) == pytest.approx(1.5)

    def test_division_por_cero_retorna_uno(self):
        assert _calcular_nivel_congestion(240, 0) == 1.0

    def test_free_flow_none_retorna_uno(self):
        assert _calcular_nivel_congestion(180, None) == 1.0

    def test_current_time_none_retorna_none(self):
        assert _calcular_nivel_congestion(None, 120) is None

    def test_ambos_none_retorna_none(self):
        assert _calcular_nivel_congestion(None, None) is None

    def test_flujo_libre_igual_actual_es_uno(self):
        assert _calcular_nivel_congestion(120, 120) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Fixtures compartidos
# ---------------------------------------------------------------------------
@pytest.fixture
def respuesta_valida():
    return {
        "id_calle_nuestro": "salvador_nava",
        "json_crudo": {
            "flowSegmentData": {
                "currentSpeed": 45,
                "freeFlowSpeed": 60,
                "currentTravelTime": 180,
                "freeFlowTravelTime": 120,
                "coordinates": {
                    "coordinate": [
                        {"latitude": 22.135, "longitude": -100.992},
                        {"latitude": 22.136, "longitude": -100.993},
                    ]
                },
            }
        },
    }


@pytest.fixture
def respuesta_json_vacio():
    return {"id_calle_nuestro": "himno_nacional", "json_crudo": {}}


@pytest.fixture
def respuesta_free_flow_cero():
    return {
        "id_calle_nuestro": "venustiano_carranza",
        "json_crudo": {
            "flowSegmentData": {
                "currentSpeed": 30,
                "freeFlowSpeed": 60,
                "currentTravelTime": 240,
                "freeFlowTravelTime": 0,
                "coordinates": {"coordinate": []},
            }
        },
    }


@pytest.fixture
def respuesta_claves_ausentes():
    return {
        "id_calle_nuestro": "mariano_jimenez",
        "json_crudo": {
            "flowSegmentData": {
                # currentSpeed, currentTravelTime y freeFlowTravelTime ausentes
                "freeFlowSpeed": 60,
                "coordinates": {"coordinate": []},
            }
        },
    }


# ---------------------------------------------------------------------------
# Pruebas de integración: transform()
# ---------------------------------------------------------------------------
class TestTransform:
    def test_registro_valido_produce_dos_paquetes(self, respuesta_valida):
        estaticos, dinamicos = transform([respuesta_valida])
        assert len(estaticos) == 1
        assert len(dinamicos) == 1

    def test_nivel_congestion_calculado_correctamente(self, respuesta_valida):
        _, dinamicos = transform([respuesta_valida])
        _timestamp, _id, _vel, _tiempo, congestion = dinamicos[0]
        assert congestion == pytest.approx(1.5)

    def test_json_vacio_es_omitido_y_no_rompe_ejecucion(self, respuesta_json_vacio):
        estaticos, dinamicos = transform([respuesta_json_vacio])
        assert len(estaticos) == 0
        assert len(dinamicos) == 0

    def test_free_flow_cero_asigna_congestion_uno(self, respuesta_free_flow_cero):
        _, dinamicos = transform([respuesta_free_flow_cero])
        assert len(dinamicos) == 1
        *_, congestion = dinamicos[0]
        assert congestion == 1.0

    def test_claves_ausentes_producen_none_sin_excepcion(self, respuesta_claves_ausentes):
        estaticos, dinamicos = transform([respuesta_claves_ausentes])
        assert len(dinamicos) == 1
        _, _, velocidad, tiempo, congestion = dinamicos[0]
        assert velocidad is None
        assert tiempo is None
        assert congestion is None

    def test_paquete_estatico_incluye_coordenadas_y_wkt(self, respuesta_valida):
        estaticos, _ = transform([respuesta_valida])
        _id, _nombre, coordenadas, wkt = estaticos[0]
        assert isinstance(coordenadas, list)
        assert len(coordenadas) == 2
        assert wkt == "LINESTRING(-100.992 22.135, -100.993 22.136)"

    def test_sin_coordenadas_wkt_es_none(self, respuesta_free_flow_cero):
        estaticos, _ = transform([respuesta_free_flow_cero])
        assert estaticos[0][3] is None

    def test_lista_mixta_solo_validos_son_procesados(
        self, respuesta_valida, respuesta_json_vacio, respuesta_free_flow_cero
    ):
        entrada = [respuesta_valida, respuesta_json_vacio, respuesta_free_flow_cero]
        estaticos, dinamicos = transform(entrada)
        # json_vacio debe ser descartado; los otros dos deben pasar
        assert len(estaticos) == 2
        assert len(dinamicos) == 2

    def test_id_calle_se_propaga_correctamente(self, respuesta_valida):
        estaticos, dinamicos = transform([respuesta_valida])
        id_estatico = estaticos[0][0]
        id_dinamico = dinamicos[0][1]
        assert id_estatico == "salvador_nava"
        assert id_dinamico == "salvador_nava"

    def test_timestamp_tiene_formato_utc(self, respuesta_valida):
        _, dinamicos = transform([respuesta_valida])
        timestamp = dinamicos[0][0]
        assert "+00" in timestamp
