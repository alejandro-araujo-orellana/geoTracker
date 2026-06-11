import json
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _calcular_nivel_congestion(current_travel_time, free_flow_travel_time) -> float | None:
    """
    Ratio entre tiempo real y tiempo en flujo libre.
    Devuelve 1.0 si freeFlowTravelTime es 0 o None (división por cero).
    Devuelve None si currentTravelTime no está disponible.
    """
    if current_travel_time is None:
        return None
    if not free_flow_travel_time:
        return 1.0
    return current_travel_time / free_flow_travel_time


def transform(respuestas_crudas: list) -> tuple[list, list]:
    """
    Transforma la lista de respuestas crudas de extractor.py en dos paquetes
    listos para ser inyectados en la base de datos por loader.py.

    Args:
        respuestas_crudas: Lista de dicts con claves 'id_calle_nuestro' y 'json_crudo'.

    Returns:
        (lista_datos_estaticos, lista_datos_dinamicos)
        - lista_datos_estaticos: tuplas para dim_segmentos_viales.
        - lista_datos_dinamicos: tuplas para fact_lecturas_trafico.
    """
    lista_datos_estaticos: list[tuple] = []
    lista_datos_dinamicos: list[tuple] = []

    for elemento in respuestas_crudas:
        id_calle = elemento.get("id_calle_nuestro")
        json_crudo = elemento.get("json_crudo")

        # Omitir si la respuesta está vacía o no contiene la estructura esperada
        if not json_crudo or "flowSegmentData" not in json_crudo:
            logger.warning("Calle '%s': json_crudo vacío o sin clave 'flowSegmentData'. Omitiendo registro.", id_calle)
            continue

        flow_data = json_crudo["flowSegmentData"]

        # Extracción defensiva con .get() — evita KeyError si la API cambia (ARCHITECTURE.md §4)
        current_speed = flow_data.get("currentSpeed")
        free_flow_speed = flow_data.get("freeFlowSpeed")
        current_travel_time = flow_data.get("currentTravelTime")
        free_flow_travel_time = flow_data.get("freeFlowTravelTime")

        if current_speed is None or free_flow_speed is None:
            logger.warning("Calle '%s': métricas de velocidad ausentes (currentSpeed/freeFlowSpeed = None).", id_calle)

        if current_travel_time is None or free_flow_travel_time is None:
            logger.warning("Calle '%s': métricas de tiempo ausentes (currentTravelTime/freeFlowTravelTime = None).", id_calle)

        # Cálculo del índice de congestión en tiempo real
        nivel_congestion = _calcular_nivel_congestion(current_travel_time, free_flow_travel_time)

        # Timestamp UTC en formato ISO 8601 compatible con TIMESTAMPTZ de Postgres
        timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")

        # Extracción de geometría para JSONB — nunca va a fact_lecturas_trafico (ARCHITECTURE.md §4)
        coordenadas_raw = flow_data.get("coordinates", {}).get("coordinate", [])
        #geometria_json_string = json.dumps(coordenadas_raw)

        # Tupla para dim_segmentos_viales
        paquete_estatico = (id_calle, id_calle, coordenadas_raw) 
	#geometria_json_string)

        # Tupla para fact_lecturas_trafico
        paquete_dinamico = (timestamp_utc, id_calle, current_speed, current_travel_time, nivel_congestion)

        lista_datos_estaticos.append(paquete_estatico)
        lista_datos_dinamicos.append(paquete_dinamico)

        logger.info(
            "Calle '%s' transformada | velocidad: %s km/h | congestión: %s",
            id_calle,
            current_speed,
            f"{nivel_congestion:.3f}" if nivel_congestion is not None else "N/A",
        )

    logger.info(
        "Transformación completa: %d registros estáticos y %d registros dinámicos listos para carga.",
        len(lista_datos_estaticos),
        len(lista_datos_dinamicos),
    )

    return lista_datos_estaticos, lista_datos_dinamicos


# --- Bloque de prueba local (datos simulados) ---
if __name__ == "__main__":
    mock_respuestas = [
        {
            "id_calle_nuestro": "salvador_nava",
            "json_crudo": {
                "flowSegmentData": {
                    "currentSpeed": 45,
                    "freeFlowSpeed": 60,
                    "currentTravelTime": 180,
                    "freeFlowTravelTime": 120,
                    "coordinates": {
                        "coordinate": [
                            {"latitude": 22.135, "longitude": -100.992},
                            {"latitude": 22.136, "longitude": -100.993},
                        ]
                    },
                }
            },
        },
        {
            "id_calle_nuestro": "himno_nacional",
            "json_crudo": {},  # Caso de error: respuesta vacía
        },
        {
            "id_calle_nuestro": "venustiano_carranza",
            "json_crudo": {
                "flowSegmentData": {
                    "currentSpeed": 30,
                    "freeFlowSpeed": 60,
                    "currentTravelTime": 240,
                    "freeFlowTravelTime": 0,  # Caso borde: división por cero
                    "coordinates": {"coordinate": []},
                }
            },
        },
    ]

    estaticos, dinamicos = transform(mock_respuestas)

    print("\n--- Paquetes estáticos (dim_segmentos_viales) ---")
    for registro in estaticos:
        print(registro)

    print("\n--- Paquetes dinámicos (fact_lecturas_trafico) ---")
    for registro in dinamicos:
        print(registro)
