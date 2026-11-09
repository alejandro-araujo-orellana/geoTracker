"""
extractor_tiles.py
==================
Fuente PRINCIPAL de tráfico (D8, docs/DECISIONES.md): TomTom Traffic Flow
vector tiles, estilo 'relative'.

Cada tile trae, para todas las vías visibles a ese zoom, tramos (LineString)
con:
    traffic_level  velocidad actual / velocidad en flujo libre (0..1)
    road_closure   True si la vía está cerrada (traffic_level = 0)
Las vías de doble sentido traen una línea por sentido, dibujada en el
sentido de circulación (D9).

Cuota: 200 000 tiles/mes (contador propio en consumo_api). A zoom 14 la
zona de estudio son 49 tiles por pasada.
"""

import asyncio
import logging
import math

import aiohttp
import mapbox_vector_tile

import zona

logger = logging.getLogger(__name__)

URL_TILE = "https://api.tomtom.com/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.pbf"
TAGS = "[traffic_level,road_closure,road_category]"
CAPA = "Traffic flow"
MAX_INTENTOS = 2


# ---------------------------------------------------------------------------
# Geometría de tiles (Web Mercator, esquema XYZ)
# ---------------------------------------------------------------------------
def tile_de(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tiles_de_la_zona(z: int) -> list[tuple[int, int]]:
    x0, y0 = tile_de(zona.LAT_MAX, zona.LON_MIN, z)  # noroeste
    x1, y1 = tile_de(zona.LAT_MIN, zona.LON_MAX, z)  # sureste
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def a_lonlat(z: int, x: int, y: int, px: float, py: float, extent: int) -> tuple[float, float]:
    """Coordenadas internas del tile (py medido desde arriba) → lon/lat WGS84."""
    n = 2 ** z
    gx, gy = x + px / extent, y + py / extent
    lon = gx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / n))))
    return lon, lat


def decodificar_tile(contenido: bytes, z: int, x: int, y: int) -> list[dict]:
    """
    Tile MVT → tramos {nivel, cerrada, categoria, wkt}. Las MultiLineString se
    separan en líneas; los tramos sin traffic_level se descartan.
    """
    capa = mapbox_vector_tile.decode(contenido, default_options={"y_coord_down": True}).get(CAPA)
    if not capa:
        return []
    extent = capa.get("extent", 4096)

    tramos = []
    for feature in capa["features"]:
        props = feature["properties"]
        nivel = props.get("traffic_level")
        if nivel is None:
            continue
        geom = feature["geometry"]
        lineas = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
        for linea in lineas:
            if len(linea) < 2:
                continue
            puntos = (a_lonlat(z, x, y, px, py, extent) for px, py in linea)
            tramos.append({
                "nivel": float(nivel),
                "cerrada": bool(props.get("road_closure", False)),
                "categoria": props.get("road_category"),
                "wkt": "LINESTRING(" + ", ".join(f"{lon!r} {lat!r}" for lon, lat in puntos) + ")",
            })
    return tramos


# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------
async def _descargar_tile(session, api_key, z, x, y, cupo, semaforo) -> list[dict] | None:
    async with semaforo:
        for intento in range(MAX_INTENTOS):
            if not cupo.tomar():
                return None
            try:
                async with session.get(
                    URL_TILE.format(z=z, x=x, y=y),
                    params={"key": api_key, "tags": TAGS},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return decodificar_tile(await resp.read(), z, x, y)
                    if resp.status == 403:
                        logger.error("Tile %d/%d/%d: HTTP 403 (cuota de tiles agotada o key inválida).", z, x, y)
                        cupo.bloquear()
                        return None
                    if resp.status not in (429, 500, 502, 503, 504):
                        logger.error("Tile %d/%d/%d: HTTP %d, se omite.", z, x, y, resp.status)
                        return None
                    logger.warning("Tile %d/%d/%d: HTTP %d (intento %d).", z, x, y, resp.status, intento + 1)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("Tile %d/%d/%d: %s (intento %d).", z, x, y, type(e).__name__, intento + 1)
            await asyncio.sleep(1 + intento)
        return None


async def descargar_pasada(session, api_key: str, z: int, tiles: list[tuple[int, int]], cupo,
                           limite_concurrencia: int = 8) -> tuple[list[dict], int]:
    """Descarga los tiles dados. Devuelve (tramos, tiles descargados con éxito)."""
    semaforo = asyncio.Semaphore(limite_concurrencia)
    resultados = await asyncio.gather(
        *(_descargar_tile(session, api_key, z, x, y, cupo, semaforo) for x, y in tiles)
    )
    exitosos = [r for r in resultados if r is not None]
    return [t for r in exitosos for t in r], len(exitosos)


async def extraer(api_key: str, z: int, cupo, limite_concurrencia: int = 8) -> tuple[list[dict], int]:
    """Pasada completa sobre la zona de estudio."""
    tiles = tiles_de_la_zona(z)
    async with aiohttp.ClientSession() as session:
        tramos, ok = await descargar_pasada(session, api_key, z, tiles, cupo, limite_concurrencia)
    logger.info("Tiles: %d/%d descargados → %d tramos de tráfico.", ok, len(tiles), len(tramos))
    return tramos, ok
