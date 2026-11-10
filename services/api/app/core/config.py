"""Configuración de la API desde variables de entorno (las mismas DB_* que el ETL)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def db_url(self) -> str:
        return (f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
