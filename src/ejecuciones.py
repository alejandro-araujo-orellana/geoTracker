"""
ejecuciones.py
==============
Registro de cada ciclo del ETL en la tabla ejecuciones_etl (migración 015, D41).

main.ciclo_etl crea un ResumenCiclo al empezar, cada fuente anota lo que hizo y, al terminar
(bien, sin datos o con excepción), se guarda una fila. Es observabilidad: si guardarla falla,
el ciclo no falla (main lo registra en el log y sigue).
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

MAX_ERROR = 2000  # caracteres; una traza larga no aporta en la tabla (está en el log del pod)


@dataclass
class ResumenCiclo:
    inicio: datetime
    fin: datetime | None = None
    estado: str = "sin_datos"
    aristas_medidas: int = 0
    tiles_peticiones: int = 0
    tiles_bloqueado: bool = False
    sd_peticiones: int = 0
    sd_bloqueado: bool = False
    sd_cargado: bool = False
    predictor: bool = False
    error: str | None = None
    pod: str | None = None

    @classmethod
    def iniciar(cls) -> "ResumenCiclo":
        # En Kubernetes HOSTNAME es el nombre del pod: lleva directo a `kubectl logs`.
        return cls(inicio=datetime.now(timezone.utc), pod=os.environ.get("HOSTNAME"))

    def anotar_error(self, etapa: str, exc: BaseException) -> None:
        nuevo = f"{etapa}: {type(exc).__name__}: {exc}"
        texto = nuevo if not self.error else f"{self.error} | {nuevo}"
        self.error = texto[:MAX_ERROR]

    def terminar(self) -> None:
        self.fin = datetime.now(timezone.utc)


_SQL = text("""
    INSERT INTO ejecuciones_etl (inicio, fin, estado, aristas_medidas, tiles_peticiones, tiles_bloqueado,
                                 sd_peticiones, sd_bloqueado, sd_cargado, predictor, error, pod)
    VALUES (:inicio, :fin, :estado, :aristas_medidas, :tiles_peticiones, :tiles_bloqueado,
            :sd_peticiones, :sd_bloqueado, :sd_cargado, :predictor, :error, :pod)
    ON CONFLICT (inicio) DO NOTHING
""")


def registrar(engine, resumen: ResumenCiclo) -> None:
    """Guarda la fila del ciclo. Idempotente por `inicio`."""
    with engine.begin() as conn:
        conn.execute(_SQL, {
            "inicio": resumen.inicio, "fin": resumen.fin or datetime.now(timezone.utc),
            "estado": resumen.estado, "aristas_medidas": resumen.aristas_medidas,
            "tiles_peticiones": resumen.tiles_peticiones, "tiles_bloqueado": resumen.tiles_bloqueado,
            "sd_peticiones": resumen.sd_peticiones, "sd_bloqueado": resumen.sd_bloqueado,
            "sd_cargado": resumen.sd_cargado, "predictor": resumen.predictor,
            "error": resumen.error, "pod": resumen.pod,
        })
