"""
presupuesto.py
==============
Pool de peticiones a TomTom Flow Segment Data (B.2, docs/ROUTING.md).

La cuota gratuita es MENSUAL (20 000 peticiones). Este módulo decide cuántas
peticiones puede gastar cada ciclo del ETL sin pasarse:

    presupuesto del día = (cuota × margen − usado antes de hoy) / días que quedan del mes
    cupo del ciclo      = presupuesto del día × peso(hora) / Σ pesos del día completo
                          (sin superar lo que queda hoy)

- Lo no gastado un día se reparte automáticamente entre los siguientes.
- Pesos por hora local: pico 3, valle 1, madrugada 0.2.
- Cuenta CADA intento HTTP (los reintentos también gastan cuota).
- Un 403 (cuota agotada o key inválida) bloquea el resto del día.

El consumo se persiste por día en la tabla consumo_api (migración 009) para
que sobreviva a reinicios del contenedor.
"""

import calendar
import logging
from datetime import date, datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)

API_FLOW_SEGMENT = "tomtom_flow_segment"

PESO_PICO = 3.0
PESO_VALLE = 1.0
PESO_MADRUGADA = 0.2
HORAS_MADRUGADA = range(0, 6)


# ---------------------------------------------------------------------------
# Cálculo (funciones puras)
# ---------------------------------------------------------------------------
def presupuesto_dia(cuota_mensual: int, margen: float, usado_antes_de_hoy: int, hoy: date) -> int:
    dias_mes = calendar.monthrange(hoy.year, hoy.month)[1]
    dias_restantes = dias_mes - hoy.day + 1
    disponible = cuota_mensual * margen - usado_antes_de_hoy
    return max(0, int(disponible // dias_restantes))


def peso_hora(hora_local: int, horas_pico: set[int]) -> float:
    if hora_local in horas_pico:
        return PESO_PICO
    if hora_local in HORAS_MADRUGADA:
        return PESO_MADRUGADA
    return PESO_VALLE


def cupo_ciclo(presupuesto_del_dia: int, restante_hoy: int, hora_local: int,
               intervalo_min: int, horas_pico: set[int]) -> int:
    ciclos_por_hora = 60 / intervalo_min
    suma_pesos_dia = sum(peso_hora(h, horas_pico) for h in range(24)) * ciclos_por_hora
    cupo = round(presupuesto_del_dia * peso_hora(hora_local, horas_pico) / suma_pesos_dia)
    if presupuesto_del_dia > 0 and cupo == 0:
        cupo = 1
    return max(0, min(cupo, restante_hoy))


def seleccionar_puntos(coordenadas: dict, ultima_lectura: dict, n: int) -> dict:
    """
    Los n puntos a consultar en este ciclo: primero los nunca medidos, luego
    los de lectura más antigua (rotación justa). Desempate por nombre.
    """
    if n <= 0:
        return {}
    orden = sorted(
        coordenadas,
        key=lambda nombre: (ultima_lectura.get(nombre) is not None,
                            ultima_lectura.get(nombre) or datetime.min, nombre),
    )
    return {nombre: coordenadas[nombre] for nombre in orden[:n]}


class Cupo:
    """Contador de peticiones de un ciclo. asyncio es monohilo: tomar() es atómico."""

    def __init__(self, limite: int):
        if limite < 0:
            raise ValueError("El límite del cupo no puede ser negativo")
        self.limite = limite
        self.usadas = 0
        self.bloqueado = False

    def tomar(self) -> bool:
        if self.bloqueado or self.usadas >= self.limite:
            return False
        self.usadas += 1
        return True

    def bloquear(self) -> None:
        self.bloqueado = True


# ---------------------------------------------------------------------------
# Persistencia (tabla consumo_api)
# ---------------------------------------------------------------------------
def leer_consumo(engine, hoy: date, api: str = API_FLOW_SEGMENT) -> tuple[int, int, bool]:
    """(usado en el mes antes de hoy, usado hoy, bloqueado hoy)."""
    sql = text("""
        SELECT
            COALESCE(SUM(peticiones) FILTER (WHERE fecha < :hoy), 0)  AS antes,
            COALESCE(SUM(peticiones) FILTER (WHERE fecha = :hoy), 0)  AS hoy,
            COALESCE(BOOL_OR(bloqueado) FILTER (WHERE fecha = :hoy), FALSE) AS bloqueado
        FROM consumo_api
        WHERE api = :api
          AND fecha >= date_trunc('month', CAST(:hoy AS date))
          AND fecha <= :hoy
    """)
    with engine.connect() as conn:
        fila = conn.execute(sql, {"hoy": hoy, "api": api}).one()
    return int(fila.antes), int(fila.hoy), bool(fila.bloqueado)


def registrar_consumo(engine, hoy: date, peticiones: int, bloqueado: bool,
                      api: str = API_FLOW_SEGMENT) -> None:
    sql = text("""
        INSERT INTO consumo_api (fecha, api, peticiones, bloqueado)
        VALUES (:hoy, :api, :n, :b)
        ON CONFLICT (fecha, api) DO UPDATE SET
            peticiones = consumo_api.peticiones + EXCLUDED.peticiones,
            bloqueado  = consumo_api.bloqueado OR EXCLUDED.bloqueado
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"hoy": hoy, "api": api, "n": peticiones, "b": bloqueado})


def ultimas_lecturas(engine) -> dict:
    sql = text("SELECT id_segmento, max(timestamp_lectura) FROM fact_lecturas_trafico GROUP BY 1")
    with engine.connect() as conn:
        return dict(conn.execute(sql).fetchall())
