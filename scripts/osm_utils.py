"""
osm_utils.py
============
Funciones puras para normalizar atributos de aristas de osmnx.
Sin dependencias pesadas, para poder probarlas sin red ni base de datos.
"""

import hashlib
import math
import re

_MASCARA_BIGINT_POSITIVO = 0x7FFF_FFFF_FFFF_FFFF
_PATRON_NUMERO = re.compile(r"\d+(?:\.\d+)?")
_MPH_A_KMH = 1.609344


def _es_nulo(valor) -> bool:
    return valor is None or (isinstance(valor, float) and math.isnan(valor))


# Jerarquía vial de OSM, de mayor a menor importancia. Cuando osmnx fusiona
# vías de distinto tipo en una arista, se conserva el tipo de mayor jerarquía.
JERARQUIA_VIAL = (
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "track",
)
_RANGO_VIAL = {tipo: i for i, tipo in enumerate(JERARQUIA_VIAL)}


# Nota sobre listas: cuando osmnx simplifica el grafo y fusiona vías con
# atributos distintos, guarda una lista construida desde un set. Su orden
# depende de PYTHONHASHSEED y cambia entre procesos, así que NUNCA se toma
# "el primero": cada normalizador elige de forma independiente del orden.


def id_determinista(u: int, v: int, k: int) -> int:
    """
    ID estable de una arista (u, v, k) que cabe en un bigint positivo de Postgres.

    A diferencia de hash() de Python —aleatorio por proceso (PYTHONHASHSEED)—
    produce el mismo valor en cada ejecución.
    """
    clave = f"{int(u)}_{int(v)}_{int(k)}".encode()
    digest = hashlib.blake2b(clave, digest_size=8).digest()
    return int.from_bytes(digest, "big") & _MASCARA_BIGINT_POSITIVO


def _limpiar_texto(valor) -> str | None:
    if _es_nulo(valor):
        return None
    texto = str(valor).strip()
    return texto if texto and texto.lower() != "nan" else None


def normalizar_nombre(valor) -> str | None:
    """Texto o lista de textos → un nombre. Con lista se toma el primero en orden alfabético."""
    if isinstance(valor, (list, tuple)):
        nombres = sorted(n for n in map(_limpiar_texto, valor) if n)
        return nombres[0] if nombres else None
    return _limpiar_texto(valor)


def normalizar_tipo(valor) -> str:
    """Tipo de vía o lista de tipos → el de mayor jerarquía (desconocidos al final, en orden alfabético)."""
    if isinstance(valor, (list, tuple)):
        tipos = [str(t) for t in valor if not _es_nulo(t)]
        if not tipos:
            return "None"
        return min(tipos, key=lambda t: (_RANGO_VIAL.get(t, len(_RANGO_VIAL)), t))
    return str(valor)


def parsear_maxspeed(valor) -> float | None:
    """'50', '50 km/h', '30 mph', ['40', '60'], NaN → km/h o None. Con lista se toma el menor (conservador)."""
    candidatos = valor if isinstance(valor, (list, tuple)) else [valor]
    velocidades = []
    for candidato in candidatos:
        if _es_nulo(candidato):
            continue
        texto = str(candidato).lower()
        numero = _PATRON_NUMERO.search(texto)
        if not numero:
            continue
        kmh = float(numero.group())
        if "mph" in texto:
            kmh *= _MPH_A_KMH
        if kmh > 0:
            velocidades.append(round(kmh, 1))
    return min(velocidades) if velocidades else None


def normalizar_oneway(valor) -> bool | None:
    """osmnx entrega bool, lista de bool o (raramente) texto 'yes'/'no'."""
    if isinstance(valor, (list, tuple)):
        valores = [normalizar_oneway(v) for v in valor]
        valores = [v for v in valores if v is not None]
        return any(valores) if valores else None
    if _es_nulo(valor):
        return None
    if isinstance(valor, str):
        return valor.strip().lower() in {"yes", "true", "1", "-1"}
    return bool(valor)
