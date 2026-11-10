from datetime import date, datetime, timezone

import pytest

from presupuesto import Cupo, cupo_ciclo, cupo_pasada, peso_hora, presupuesto_dia, seleccionar_puntos

PICO = {7, 8, 9, 13, 14, 17, 18, 19, 20}


class TestPresupuestoDia:
    def test_reparte_lo_que_queda_del_mes_entre_los_dias_restantes(self):
        # 20 000 × 0.9 = 18 000; noviembre tiene 30 días; el día 1 quedan 30 días
        assert presupuesto_dia(20_000, 0.9, usado_antes_de_hoy=0, hoy=date(2026, 11, 1)) == 600

    def test_lo_no_gastado_se_reparte_en_los_dias_siguientes(self):
        # día 21: quedan 10 días; se gastaron 12 000 → (18 000 - 12 000) / 10
        assert presupuesto_dia(20_000, 0.9, usado_antes_de_hoy=12_000, hoy=date(2026, 11, 21)) == 600
        assert presupuesto_dia(20_000, 0.9, usado_antes_de_hoy=6_000, hoy=date(2026, 11, 21)) == 1_200

    def test_ultimo_dia_del_mes(self):
        assert presupuesto_dia(20_000, 0.9, usado_antes_de_hoy=17_500, hoy=date(2026, 11, 30)) == 500

    def test_cuota_agotada_es_cero(self):
        assert presupuesto_dia(20_000, 0.9, usado_antes_de_hoy=19_000, hoy=date(2026, 11, 10)) == 0


class TestPesoHora:
    def test_pico_valle_madrugada(self):
        assert peso_hora(18, PICO) == 3.0
        assert peso_hora(11, PICO) == 1.0
        assert peso_hora(3, PICO) == 0.2


class TestCupoCiclo:
    def test_pico_recibe_el_triple_que_valle(self):
        pico = cupo_ciclo(600, restante_hoy=600, hora_local=18, intervalo_min=15, horas_pico=PICO)
        valle = cupo_ciclo(600, restante_hoy=600, hora_local=11, intervalo_min=15, horas_pico=PICO)
        assert pico == 12 and valle == 4

    def test_madrugada_recibe_al_menos_una_si_hay_presupuesto(self):
        assert cupo_ciclo(600, restante_hoy=600, hora_local=3, intervalo_min=15, horas_pico=PICO) == 1

    def test_la_suma_del_dia_no_supera_mucho_el_presupuesto(self):
        total = sum(
            cupo_ciclo(600, restante_hoy=600, hora_local=h, intervalo_min=15, horas_pico=PICO)
            for h in range(24) for _ in range(4)
        )
        assert 560 <= total <= 620

    def test_nunca_supera_lo_que_queda_hoy(self):
        assert cupo_ciclo(600, restante_hoy=5, hora_local=18, intervalo_min=15, horas_pico=PICO) == 5
        assert cupo_ciclo(600, restante_hoy=0, hora_local=18, intervalo_min=15, horas_pico=PICO) == 0


class TestCupoPasada:
    """Tiles: una pasada parcial deja huecos en el mapa, así que es todo o nada."""

    def test_alcanza_para_la_pasada_completa(self):
        assert cupo_pasada(presupuesto_del_dia=6_000, usado_hoy=100, tiles_por_pasada=49) == 49

    def test_no_alcanza_no_se_hace(self):
        assert cupo_pasada(presupuesto_del_dia=6_000, usado_hoy=5_960, tiles_por_pasada=49) == 0

    def test_justo_lo_necesario(self):
        assert cupo_pasada(presupuesto_del_dia=6_000, usado_hoy=5_951, tiles_por_pasada=49) == 49

    def test_cuota_mensual_de_tiles_alcanza_para_todos_los_ciclos(self):
        # 200 000 × 0.9 / 30 días = 6 000/día ≥ 96 ciclos × 49 tiles = 4 704
        del_dia = presupuesto_dia(200_000, 0.9, usado_antes_de_hoy=0, hoy=date(2026, 11, 1))
        assert del_dia >= 96 * 49


class TestSeleccionarPuntos:
    PUNTOS = {"a": "1,1", "b": "2,2", "c": "3,3", "d": "4,4"}

    def test_primero_los_nunca_medidos_luego_los_mas_antiguos(self):
        ultimas = {
            "a": datetime(2026, 7, 1, 10, tzinfo=timezone.utc),
            "b": datetime(2026, 6, 1, 10, tzinfo=timezone.utc),
            "c": datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
        }
        assert list(seleccionar_puntos(self.PUNTOS, ultimas, 3)) == ["d", "b", "a"]

    def test_devuelve_coordenadas(self):
        assert seleccionar_puntos(self.PUNTOS, {}, 1) == {"a": "1,1"}

    def test_cupo_mayor_que_puntos_devuelve_todos(self):
        assert len(seleccionar_puntos(self.PUNTOS, {}, 99)) == 4

    def test_cupo_cero(self):
        assert seleccionar_puntos(self.PUNTOS, {}, 0) == {}


class TestCupo:
    def test_tomar_hasta_el_limite(self):
        cupo = Cupo(2)
        assert cupo.tomar() and cupo.tomar()
        assert not cupo.tomar()
        assert cupo.usadas == 2

    def test_bloquear_corta_todo(self):
        cupo = Cupo(10)
        cupo.tomar()
        cupo.bloquear()
        assert not cupo.tomar()
        assert cupo.bloqueado and cupo.usadas == 1

    def test_limite_negativo_es_invalido(self):
        with pytest.raises(ValueError):
            Cupo(-1)
