"""Routers de la versión 1 de la API, montados en settings.api_v1_prefix por app.main."""

from fastapi import APIRouter

from app.rutas.router import router as rutas_router

api_router_v1 = APIRouter()
api_router_v1.include_router(rutas_router)
