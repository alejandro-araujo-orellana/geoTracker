"""
main.py
=======
Punto de entrada de la aplicación FastAPI.
Arma la app, monta los routers versionados bajo /api/v1, y expone un
endpoint /health para que el load balancer / orquestador sepa si esta
instancia está sana.
"""

from fastapi import FastAPI
from app.api.v1 import api_router_v1
from app.core.config import get_settings
from app.core.database import verificar_conexiones

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    #doc interactiva en /docs swagger y /redoc
)

app.include_router(api_router_v1, prefix=settings.api_v1_prefix)


@app.get("/health", tags=["infra"], summary="Estado de la instancia")
def health() -> dict:
    """
    Comprobación de salud. El gateway / orquestador la llama periódicamente;
    si falla, retira esta instancia de la rotación de tráfico.
    Verifica que ambos engines (lectura y escritura) respondan.
    """
    estado_db = verificar_conexiones()
    todo_ok = all(v == "ok" for v in estado_db.values())
    return {
        "status": "ok" if todo_ok else "degraded",
        "app": settings.app_name,
        "database": estado_db,
    }
