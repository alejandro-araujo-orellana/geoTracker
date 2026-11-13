"""
Acceso por token (D26): la API no es pública (D8, D23). Cada petición a /api/v1 debe
traer el header X-API-Key con uno de los tokens de API_TOKENS; si no, 401.
/health queda abierto: lo llaman los probes de Kubernetes y el healthcheck de Compose.
"""

import logging
import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# APIKeyHeader (y no un Header() simple) para que /docs muestre el botón "Authorize".
_esquema = APIKeyHeader(name="X-API-Key", auto_error=False, description="Token de acceso del dispositivo")


def _token_valido(recibido: str, permitidos: tuple[str, ...]) -> bool:
    # compare_digest contra TODOS (sin cortar en el primero) para no filtrar por tiempo cuál coincide.
    coincide = False
    for permitido in permitidos:
        coincide |= secrets.compare_digest(recibido.encode(), permitido.encode())
    return coincide


def verificar_token(x_api_key: str | None = Security(_esquema)) -> None:
    """Dependencia de FastAPI para los routers protegidos."""
    settings = get_settings()
    if settings.api_auth_desactivada:
        return
    if not x_api_key or not _token_valido(x_api_key, settings.tokens):
        # Nunca registrar el token recibido.
        logger.warning("Petición rechazada: %s", "sin X-API-Key" if not x_api_key else "token inválido")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Token ausente o inválido (header X-API-Key)",
            headers={"WWW-Authenticate": "ApiKey"},
        )
