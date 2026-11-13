"""Configuración de la API desde variables de entorno (las mismas DB_* que el ETL)."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LONGITUD_MIN_TOKEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "geoTracker API"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    db_user: str
    db_password: str
    db_host: str = "timescaledb"
    db_port: int = 5432
    db_name: str

    # Una coordenada a más de esto del nodo más cercano está fuera de la red vial cargada.
    distancia_max_encaje_m: float = 300.0

    # Acceso por token (D26): header X-API-Key. Uno por dispositivo, separados por coma,
    # para poder revocar uno sin cambiar los demás. Sin tokens la API no arranca, salvo
    # que se desactive a propósito (solo desarrollo).
    api_tokens: str = ""
    api_auth_desactivada: bool = False

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.api_tokens.split(",") if t.strip())

    @model_validator(mode="after")
    def _exigir_tokens(self) -> "Settings":
        if self.api_auth_desactivada:
            return self
        if not self.tokens:
            raise ValueError("API_TOKENS vacío: define al menos un token o API_AUTH_DESACTIVADA=true")
        cortos = [i for i, t in enumerate(self.tokens) if len(t) < LONGITUD_MIN_TOKEN]
        if cortos:
            raise ValueError(f"Tokens con menos de {LONGITUD_MIN_TOKEN} caracteres en las posiciones {cortos} "
                             "(genera uno con: openssl rand -hex 32)")
        return self

    @property
    def db_url(self) -> str:
        return (f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
