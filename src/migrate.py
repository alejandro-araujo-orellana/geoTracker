"""
migrate.py
==========
Aplica las migraciones SQL de db/migrations/ en orden, una sola vez cada una.

Las migraciones aplicadas se registran en la tabla schema_migrations. Cada
archivo corre en su propia transacción: si falla, se revierte y se detiene
el proceso sin marcarla como aplicada.

Uso:
    python src/migrate.py            # aplica las pendientes
    main.py lo invoca al arrancar el daemon.
"""

import logging
import re
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

DIRECTORIO_MIGRACIONES = Path(__file__).resolve().parent.parent / "db" / "migrations"
_PATRON_ARCHIVO = re.compile(r"^(\d{3})_[\w-]+\.sql$")


def listar_migraciones(directorio: Path = DIRECTORIO_MIGRACIONES) -> list[tuple[str, Path]]:
    """Devuelve [(version, ruta)] ordenado por versión. Ignora archivos que no siguen NNN_nombre.sql."""
    migraciones = []
    for ruta in directorio.glob("*.sql"):
        coincidencia = _PATRON_ARCHIVO.match(ruta.name)
        if coincidencia:
            migraciones.append((coincidencia.group(1), ruta))
        else:
            logger.warning("Archivo ignorado en migraciones (nombre inválido): %s", ruta.name)

    versiones = [v for v, _ in migraciones]
    duplicadas = {v for v in versiones if versiones.count(v) > 1}
    if duplicadas:
        raise ValueError(f"Versiones de migración duplicadas: {sorted(duplicadas)}")

    return sorted(migraciones)


def aplicar_migraciones(engine, directorio: Path = DIRECTORIO_MIGRACIONES) -> list[str]:
    """Aplica las migraciones pendientes. Devuelve las versiones aplicadas en esta llamada."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version     text PRIMARY KEY,"
            "  nombre      text NOT NULL,"
            "  aplicada_en timestamptz NOT NULL DEFAULT now()"
            ")"
        ))
        aplicadas = {fila[0] for fila in conn.execute(text("SELECT version FROM schema_migrations"))}

    nuevas = []
    for version, ruta in listar_migraciones(directorio):
        if version in aplicadas:
            continue
        logger.info("Aplicando migración %s ...", ruta.name)
        with engine.begin() as conn:
            conn.exec_driver_sql(ruta.read_text(encoding="utf-8"))
            conn.execute(
                text("INSERT INTO schema_migrations (version, nombre) VALUES (:v, :n)"),
                {"v": version, "n": ruta.name},
            )
        nuevas.append(version)

    if nuevas:
        logger.info("Migraciones aplicadas: %s", ", ".join(nuevas))
    else:
        logger.info("Esquema al día: no hay migraciones pendientes.")
    return nuevas


def configurar_rol_grafana(engine, password: str) -> None:
    """
    Habilita el login de `grafana_ro` (creado por la migración 013) con esta contraseña.
    Idempotente: correrlo con otra contraseña la rota. D33.
    """
    if not password:
        raise ValueError("Contraseña vacía para grafana_ro")
    with engine.begin() as conn:
        # ALTER ROLE no admite parámetros del lado del servidor; psycopg2 sustituye %s del
        # lado del cliente con el escapado correcto.
        conn.exec_driver_sql("ALTER ROLE grafana_ro WITH LOGIN PASSWORD %s", (password,))


if __name__ == "__main__":
    from sqlalchemy import create_engine

    import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    engine = create_engine(config.DB_URL)
    aplicar_migraciones(engine)
    if config.GRAFANA_DB_PASSWORD:
        configurar_rol_grafana(engine, config.GRAFANA_DB_PASSWORD)
        logger.info("Rol grafana_ro: login habilitado con la contraseña de GRAFANA_DB_PASSWORD.")
    else:
        logger.info("Sin GRAFANA_DB_PASSWORD: el rol grafana_ro queda sin login.")
