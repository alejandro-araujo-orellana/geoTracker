"""
fuente_pbf.py
=============
Red vial desde un extracto .osm.pbf en lugar de la API de Overpass (D30).

Por qué:
    El Overpass público (overpass-api.de) rechaza clientes automatizados. Los
    extractos .pbf (openstreetmap.fr, Geofabrik) son archivos estáticos que se
    regeneran a diario y están hechos para descargarse de forma automática.

Cómo, sin cambiar el resultado:
    osmnx.graph_from_bbox hace dos cosas: (1) pide a Overpass las vías del
    polígono que pasan el filtro de `network_type` y (2) arma el grafo con esa
    respuesta (truncar al polígono con búfer de 500 m, componente mayor,
    simplificar, truncar al polígono original). Aquí solo se reemplaza (1):
    `desde_pbf()` intercambia la descarga de Overpass por una lectura del .pbf
    que devuelve el MISMO formato JSON que Overpass, con la misma selección:
      - vías que cumplen el filtro de osmnx para `network_type`, leído del
        propio osmnx para no desincronizarse si cambia de versión;
      - vías con al menos un nodo dentro del polígono que osmnx pidió, y TODOS
        sus nodos (el `>;` de la consulta). Las vías que Overpass también
        devolvería por cruzar el polígono sin tener nodos dentro se quedan
        fuera, pero osmnx las eliminaría igual al truncar por nodos;
      - elementos ordenados por id y coordenadas con 7 decimales, como `out;`.
    El paso (2) sigue siendo el código de osmnx sin tocar.

    Usa internals de osmnx (`_overpass._download_overpass_network` y
    `_get_network_filter`): versión fijada en requirements.txt (osmnx==2.1.0).
    Si cambia, valida de nuevo contra Overpass (ver D30).
"""

import logging
import os
import re
from contextlib import contextmanager
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from unittest import mock

import numpy as np
import osmium
import requests
import shapely
from osmnx import _overpass

log = logging.getLogger(__name__)

# Extracto del estado de San Luis Potosí (~15 MB, se regenera a diario). Debe
# contener la zona de src/zona.py completa. Alternativa más pesada: Geofabrik
# https://download.geofabrik.de/north-america/mexico-latest.osm.pbf (todo México).
URL_PBF_DEFAULT = "https://download.openstreetmap.fr/extracts/north-america/mexico/san_luis_potosi-latest.osm.pbf"

_CONDICION = re.compile(r'\["([^"]+)"(?:!~"([^"]*)")?\]')


# ---------------------------------------------------------------------------
# Filtro de vías (sintaxis de Overpass que usa osmnx)
# ---------------------------------------------------------------------------
def parsear_filtro(filtro: str) -> list[tuple[str, re.Pattern | None]]:
    """
    Convierte un filtro de Overpass como '["highway"]["area"!~"yes"]' en una
    lista de (clave, patrón). patrón None = "la etiqueta existe". Solo admite
    las dos formas que usa osmnx; cualquier otra cosa es un error explícito
    (mejor fallar que filtrar distinto a Overpass sin avisar).
    """
    condiciones = []
    pos = 0
    for m in _CONDICION.finditer(filtro):
        if m.start() != pos:
            break
        clave, regex = m.groups()
        condiciones.append((clave, re.compile(regex) if regex is not None else None))
        pos = m.end()
    if pos != len(filtro) or not condiciones:
        raise ValueError(f"Filtro de Overpass no soportado: {filtro!r}")
    return condiciones


def cumple_filtro(etiquetas: dict[str, str], condiciones: list[tuple[str, re.Pattern | None]]) -> bool:
    """
    Semántica de Overpass: ["k"] exige que exista la etiqueta; ["k"!~"re"]
    pasa si la etiqueta NO existe o si su valor no contiene una coincidencia
    de la regex (búsqueda, no coincidencia completa; sensible a mayúsculas).
    """
    for clave, patron in condiciones:
        valor = etiquetas.get(clave)
        if patron is None:
            if valor is None:
                return False
        elif valor is not None and patron.search(valor):
            return False
    return True


# ---------------------------------------------------------------------------
# Lectura del .pbf → JSON con forma de respuesta de Overpass
# ---------------------------------------------------------------------------
def leer_elementos(pbf: Path, poligono, condiciones) -> dict:
    """
    Una pasada por el .pbf (con ubicaciones de nodos) y devuelve
    {"elements": [nodos..., vías...]} como la consulta
    `(way{filtro}(poly:...);>;);out;` de osmnx.
    """
    shapely.prepare(poligono)
    oeste, sur, este, norte = poligono.bounds
    etiquetas_nodo: dict[int, dict] = {}
    nodos: dict[int, dict] = {}
    vias: list[dict] = []

    for obj in osmium.FileProcessor(str(pbf)).with_locations():
        if obj.is_node():
            if len(obj.tags):
                etiquetas_nodo[obj.id] = dict(obj.tags)
            continue
        if not obj.is_way():
            continue
        etiquetas = dict(obj.tags)
        if not cumple_filtro(etiquetas, condiciones):
            continue
        refs = [(n.ref, n.lon, n.lat) for n in obj.nodes if n.location.valid()]
        if len(refs) != len(obj.nodes):
            continue  # vía cortada por el borde del extracto: no es de la zona
        lon = np.fromiter((r[1] for r in refs), float, len(refs))
        lat = np.fromiter((r[2] for r in refs), float, len(refs))
        en_caja = (lon >= oeste) & (lon <= este) & (lat >= sur) & (lat <= norte)
        if not en_caja.any() or not shapely.contains_xy(poligono, lon[en_caja], lat[en_caja]).any():
            continue
        for ref, x, y in refs:
            nodos[ref] = {"type": "node", "id": ref, "lat": round(y, 7), "lon": round(x, 7)}
        vias.append({"type": "way", "id": obj.id, "nodes": [r[0] for r in refs], "tags": etiquetas})

    for ref, nodo in nodos.items():
        if ref in etiquetas_nodo:
            nodo["tags"] = etiquetas_nodo[ref]
    elementos = [nodos[i] for i in sorted(nodos)] + sorted(vias, key=lambda v: v["id"])
    log.info(f"  → {len(vias):,} vías y {len(nodos):,} nodos del .pbf dentro del polígono con búfer")
    return {"elements": elementos}


@contextmanager
def desde_pbf(pbf: Path):
    """
    Dentro del bloque, las funciones de grafo de osmnx leen `pbf` en lugar de
    consultar Overpass. Ejemplo:
        with desde_pbf(ruta):
            grafo = ox.graph_from_bbox(bbox, network_type="drive")
    """
    def _descarga_falsa(polygon, network_type, custom_filter=None):
        if custom_filter is not None:
            raise NotImplementedError("desde_pbf solo soporta network_type, no custom_filter")
        condiciones = parsear_filtro(_overpass._get_network_filter(network_type))
        yield leer_elementos(pbf, polygon, condiciones)

    with mock.patch.object(_overpass, "_download_overpass_network", _descarga_falsa):
        yield


# ---------------------------------------------------------------------------
# Descarga del extracto
# ---------------------------------------------------------------------------
def descargar_pbf(url: str, directorio: Path, timeout: int = 120) -> Path:
    """
    Descarga `url` en `directorio` (solo si cambió: If-Modified-Since contra la
    copia local). Escribe a un temporal y renombra, así una descarga cortada
    nunca deja un .pbf a medias con el nombre bueno.
    """
    directorio.mkdir(parents=True, exist_ok=True)
    destino = directorio / url.rsplit("/", 1)[-1]
    cabeceras = {"User-Agent": "geoTracker-osmnx-loader"}
    if destino.exists():
        cabeceras["If-Modified-Since"] = formatdate(destino.stat().st_mtime, usegmt=True)

    with requests.get(url, headers=cabeceras, stream=True, timeout=timeout) as r:
        if r.status_code == 304:
            log.info(f"  → {destino.name} sin cambios desde la última descarga; se reutiliza")
            return destino
        r.raise_for_status()
        esperado = int(r.headers.get("Content-Length", 0))
        temporal = destino.with_suffix(".parcial")
        recibido = 0
        with open(temporal, "wb") as f:
            for bloque in r.iter_content(chunk_size=1 << 20):
                f.write(bloque)
                recibido += len(bloque)
        if esperado and recibido != esperado:
            temporal.unlink()
            raise IOError(f"Descarga incompleta de {url}: {recibido} de {esperado} bytes")
        temporal.replace(destino)
        if "Last-Modified" in r.headers:
            t = parsedate_to_datetime(r.headers["Last-Modified"]).timestamp()
            os.utime(destino, (t, t))
    log.info(f"  → descargado {destino.name} ({recibido / 1e6:.1f} MB, Last-Modified {r.headers.get('Last-Modified')})")
    return destino


def fecha_datos(pbf: Path) -> str | None:
    """Fecha de los datos OSM según la cabecera del .pbf (si el extracto la trae)."""
    lector = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    try:
        return lector.header().get("osmosis_replication_timestamp") or None
    finally:
        lector.close()
