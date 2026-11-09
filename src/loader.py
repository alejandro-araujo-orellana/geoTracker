import logging
from sqlalchemy import Column, Float, MetaData, Table, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.types import UserDefinedType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Definición de tablas (SQLAlchemy Core)
# Solo describen las columnas que escribe este módulo. El esquema real lo
# crean las migraciones de db/migrations/ (ver migrate.py).
# ---------------------------------------------------------------------------
class _Geometry(UserDefinedType):
    """Columna PostGIS geometry (sin depender de GeoAlchemy2)."""
    cache_ok = True

    def get_col_spec(self, **kw):
        return "geometry"


_metadata = MetaData()

_dim_segmentos_viales = Table(
    "dim_segmentos_viales",
    _metadata,
    Column("id_segmento", Text, primary_key=True),
    Column("nombre_via", Text, nullable=False),
    Column("coordenadas_geometria", JSONB),
    Column("geom", _Geometry),
)

# La UniqueConstraint incluye el tiempo para ser compatible con TimescaleDB Hypertable.
_fact_lecturas_trafico = Table(
    "fact_lecturas_trafico",
    _metadata,
    Column("timestamp_lectura", TIMESTAMP(timezone=True), nullable=False),
    Column("id_segmento", Text, nullable=False),
    Column("velocidad_actual", Float),
    Column("tiempo_recorrido", Float),
    Column("nivel_congestion", Float),
    UniqueConstraint("timestamp_lectura", "id_segmento", name="uq_lectura_por_calle"),
)

# Mapeo posicional para convertir las tuplas que entrega transformer.py
_COLUMNAS_ESTATICAS = ("id_segmento", "nombre_via", "coordenadas_geometria", "geom")
_COLUMNAS_DINAMICAS = ("timestamp_lectura", "id_segmento", "velocidad_actual", "tiempo_recorrido", "nivel_congestion")


def _normalizar(lista: list, columnas: tuple) -> list[dict]:
    """
    Acepta listas de tuplas (salida de transformer.py) o listas de dicts.
    Las convierte al formato dict que SQLAlchemy exige para bulk inserts.
    """
    if not lista:
        return []
    if isinstance(lista[0], dict):
        return lista
    return [dict(zip(columnas, fila)) for fila in lista]


def _preparar_estaticos(filas: list[dict]) -> list[dict]:
    """
    Convierte el WKT de la clave 'geom' en una expresión ST_GeomFromText(wkt, 4326)
    para que PostGIS construya la geometría dentro del mismo INSERT masivo.
    """
    preparadas = []
    for fila in filas:
        fila = dict(fila)
        wkt = fila.get("geom")
        fila["geom"] = func.ST_GeomFromText(wkt, 4326) if wkt else None
        preparadas.append(fila)
    return preparadas


def load(lista_datos_estaticos: list, lista_datos_dinamicos: list, engine) -> bool:
    """
    Inserta masivamente los datos estáticos y dinámicos en PostgreSQL/TimescaleDB.

    Los duplicados son descartados silenciosamente (ON CONFLICT DO NOTHING),
    garantizando la idempotencia del pipeline ETL.

    Args:
        lista_datos_estaticos: Tuplas/dicts para dim_segmentos_viales.
        lista_datos_dinamicos: Tuplas/dicts para fact_lecturas_trafico.
        engine: Engine de SQLAlchemy creado por config.py.

    Returns:
        True si la transacción se confirmó, False si hubo un error en la BD.
    """
    estaticos = _preparar_estaticos(_normalizar(lista_datos_estaticos, _COLUMNAS_ESTATICAS))
    dinamicos = _normalizar(lista_datos_dinamicos, _COLUMNAS_DINAMICAS)

    if not estaticos and not dinamicos:
        logger.warning("Ambas listas están vacías. No se ejecutó ninguna inserción.")
        return True

    with Session(engine) as session:
        try:
            # --- Upsert de calles (dim_segmentos_viales) ---
            # Fila nueva → se inserta. Fila existente sin geometría → se completa.
            # Fila existente con geometría → no se toca (idempotente).
            if estaticos:
                stmt_dim = insert(_dim_segmentos_viales).values(estaticos)
                stmt_dim = stmt_dim.on_conflict_do_update(
                    index_elements=["id_segmento"],
                    set_={
                        "coordenadas_geometria": stmt_dim.excluded.coordenadas_geometria,
                        "geom": stmt_dim.excluded.geom,
                    },
                    where=_dim_segmentos_viales.c.geom.is_(None),
                )
                resultado_dim = session.execute(stmt_dim)
                logger.info(
                    "dim_segmentos_viales → %d/%d filas insertadas o completadas (el resto ya existía).",
                    resultado_dim.rowcount,
                    len(estaticos),
                )

            # --- Bulk insert de series de tiempo (fact_lecturas_trafico) ---
            if dinamicos:
                stmt_fact = insert(_fact_lecturas_trafico).values(dinamicos)
                stmt_fact = stmt_fact.on_conflict_do_nothing(
                    constraint="uq_lectura_por_calle"
                )
                resultado_fact = session.execute(stmt_fact)
                logger.info(
                    "fact_lecturas_trafico → %d/%d lecturas insertadas (el resto ya existía).",
                    resultado_fact.rowcount,
                    len(dinamicos),
                )

            session.commit()
            logger.info("Transacción confirmada (commit) — carga completada exitosamente.")
            return True

        except SQLAlchemyError as e:
            session.rollback()
            logger.error(
                "Error en la base de datos. Rollback ejecutado. Ningún dato fue persistido. Detalle: %s",
                e,
            )
            return False
