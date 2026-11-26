"""
main.py
=======
Punto de entrada de la aplicación FastAPI.
Arma la app, monta los routers versionados bajo /api/v1, y expone un
endpoint /health para que el load balancer / orquestador sepa si esta
instancia está sana.
"""

import logging

from fastapi import Depends, FastAPI, Response, status
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.v1 import api_router_v1
from app.core.config import get_settings
from app.core.database import verificar_conexiones
from app.core.seguridad import verificar_token

# Falla aquí (y el contenedor no arranca) si API_TOKENS falta o es débil: D26.
settings = get_settings()
if settings.api_auth_desactivada:
    logging.getLogger(__name__).warning("API_AUTH_DESACTIVADA=true: /api/v1 queda SIN token (solo desarrollo).")

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    #doc interactiva en /docs swagger y /redoc
)

# Todo /api/v1 exige X-API-Key; /health y /docs quedan abiertos.
app.include_router(api_router_v1, prefix=settings.api_v1_prefix, dependencies=[Depends(verificar_token)])


def _estado() -> tuple[bool, dict]:
    estado_db = verificar_conexiones()
    todo_ok = all(v == "ok" for v in estado_db.values())
    return todo_ok, {
        "status": "ok" if todo_ok else "degraded",
        "app": settings.app_name,
        "database": estado_db,
    }


@app.get("/health", tags=["infra"], summary="Estado de la instancia (informativo)")
def health() -> dict:
    """
    Estado legible por humanos y por el healthcheck de Compose. Responde 200 aunque la BD
    falle (status "degraded"): por eso las sondas de Kubernetes usan /health/live y
    /health/ready, que sí reflejan el estado en el código HTTP (D32).
    """
    return _estado()[1]


@app.get("/health/live", tags=["infra"], summary="Liveness: el proceso responde")
def health_live() -> dict:
    """No consulta la BD: si la BD cae, reiniciar la API no arregla nada (D32)."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["infra"], summary="Readiness: el proceso y la BD responden")
def health_ready(response: Response) -> dict:
    """503 si la BD falla: el Service saca la réplica de la rotación sin reiniciarla (D32)."""
    todo_ok, cuerpo = _estado()
    if not todo_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return cuerpo


# /metrics para Prometheus (D32): sin token, como /health. Etiqueta por plantilla de ruta
# (nunca la URL con su query) y no cuenta las sondas ni el propio scrape. Un solo worker de
# uvicorn por contenedor: con varios, cada scrape vería los contadores de un proceso distinto.
Instrumentator(excluded_handlers=["^/health", "^/metrics$"]).instrument(app).expose(app, include_in_schema=False)
