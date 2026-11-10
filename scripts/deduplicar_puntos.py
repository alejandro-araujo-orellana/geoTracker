"""
deduplicar_puntos.py
====================
Quita de data/vialidades_maestras.json los puntos de monitoreo que devuelven
el MISMO segmento de TomTom que otro punto (B.1, docs/ROUTING.md).

Por qué: Flow Segment Data responde con el segmento completo que contiene el
punto. Con zoom=10 los segmentos son largos y varios puntos (hasta 12) caían
en el mismo: peticiones distintas, dato idéntico. Con una cuota de 20 000
peticiones/mes cada una cuenta.

Cómo: agrupa los puntos por la geometría que TomTom devolvió (guardada en
dim_segmentos_viales.geom) y conserva uno por grupo: el de más lecturas
históricas (continuidad de la serie) y, si empatan, el primero alfabético.
Los puntos nunca medidos se conservan (aún no se sabe a qué segmento caen).

Uso (desde la raíz, con la BD arriba):
    python scripts/deduplicar_puntos.py
Re-ejecutable: si no hay duplicados no cambia nada.
"""

import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUTA_JSON = BASE_DIR / "data" / "vialidades_maestras.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def deduplicar(puntos: dict, firmas: dict) -> tuple[dict, list]:
    """
    puntos: {nombre: "lat,lon"} en el orden del JSON.
    firmas: {nombre: (huella_geometria, n_lecturas)} de los puntos ya medidos.
    Devuelve (puntos_conservados en el orden original, nombres_descartados).
    """
    grupos: dict[str, list[str]] = {}
    for nombre in puntos:
        if nombre in firmas:
            grupos.setdefault(firmas[nombre][0], []).append(nombre)

    descartados = set()
    for miembros in grupos.values():
        representante = min(miembros, key=lambda n: (-firmas[n][1], n))
        descartados.update(m for m in miembros if m != representante)

    conservados = {n: p for n, p in puntos.items() if n not in descartados}
    return conservados, sorted(descartados)


def _leer_firmas(engine) -> dict:
    from sqlalchemy import text

    sql = text("""
        SELECT d.id_segmento,
               md5(ST_AsBinary(d.geom)) AS huella,
               (SELECT count(*) FROM fact_lecturas_trafico f
                 WHERE f.id_segmento = d.id_segmento) AS n
        FROM dim_segmentos_viales d
        WHERE d.geom IS NOT NULL
    """)
    with engine.connect() as conn:
        return {fila.id_segmento: (fila.huella, fila.n) for fila in conn.execute(sql)}


def main() -> None:
    from sqlalchemy import create_engine

    import config

    puntos = json.loads(RUTA_JSON.read_text(encoding="utf-8"))
    conservados, descartados = deduplicar(puntos, _leer_firmas(create_engine(config.DB_URL)))

    if not descartados:
        log.info(f"Sin duplicados: {len(puntos)} puntos, cada uno con su propio segmento.")
        return

    RUTA_JSON.write_text(json.dumps(conservados, indent=4, ensure_ascii=False), encoding="utf-8")
    log.info(f"{len(puntos)} → {len(conservados)} puntos ({len(descartados)} redundantes eliminados).")


if __name__ == "__main__":
    sys.exit(main())
