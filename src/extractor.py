import asyncio
import aiohttp
import logging
import random

logger = logging.getLogger(__name__)

async def fetch_traffic_data(session, nombre_via, coord, semaforo, api_key, base_url, max_reintentos, tiempo_base):
    """
    Realiza la petición HTTP aplicando un límite de concurrencia y un 
    mecanismo de Retroceso Exponencial con Jitter en caso de fallos.
    """
    async with semaforo:
        url = f"{base_url}?key={api_key}&point={coord}"
        
        for intento in range(max_reintentos):
            try:
                # 1. Timeout estricto: Si TomTom no responde en 10s, forzamos el error
                timeout = aiohttp.ClientTimeout(total=10)
                
                async with session.get(url, timeout=timeout) as response:
                    # Caso de Éxito
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "id_calle_nuestro": nombre_via,
                            "coordenadas_origen": coord,
                            "json_crudo": data
                        }
                    
                    # Errores Recuperables: 429 (Límite de peticiones) o 5XX (Errores del servidor de TomTom)
                    elif response.status in (429, 500, 502, 503, 504):
                        logger.warning(
                            "[%s] Intento %d/%d fallido (HTTP %d). Servidor saturado.", 
                            nombre_via, intento + 1, max_reintentos, response.status
                        )
                    
                    # Errores Fatales: 400 (Mala petición), 401 (Token inválido), 404 (No encontrado)
                    else:
                        logger.error(
                            "[%s] Error fatal HTTP %d. Abortando reintentos para esta vía.", 
                            nombre_via, response.status
                        )
                        return None
                        
            # Capturamos caídas de red, pérdida de DNS o el timeout de 10s
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    "[%s] Intento %d/%d fallido por error de red/timeout: %s", 
                    nombre_via, intento + 1, max_reintentos, type(e).__name__
                )
            
            # --- Lógica de Retroceso Exponencial (Solo llega aquí si falló y es recuperable) ---
            if intento < max_reintentos - 1:
                # Fórmula: Base * (2 ^ intento) + Ruido aleatorio (Jitter entre 0 y 1 seg)
                tiempo_espera = (tiempo_base * (2 ** intento)) + random.uniform(0, 1)
                
                logger.info("[%s] Esperando %.2fs antes de reintentar...", nombre_via, tiempo_espera)
                await asyncio.sleep(tiempo_espera)
        
        # Si el bucle 'for' termina sus iteraciones y llega aquí, se rindió.
        logger.error("[%s] Se agotaron los %d intentos. Dato descartado en este ciclo ETL.", nombre_via, max_reintentos)
        return None

async def run_extraction(
	coordenadas: dict, 
	api_key: str,
	base_url: str,
	max_reintentos: str,
	tiempo_base: float,
	limite_concurrencia: int ):
    """
    Orquesta las peticiones en paralelo para toda la topología.
    """
    # Límite de 20 peticiones simultáneas
    semaforo = asyncio.Semaphore(limite_concurrencia)
    
    async with aiohttp.ClientSession() as session:
        tareas = []
        
        for nombre_via, coord in coordenadas.items():
            tarea = asyncio.create_task(
                fetch_traffic_data(session, nombre_via, coord, semaforo, api_key, base_url, max_reintentos, tiempo_base)
            )
            tareas.append(tarea)
        
        logger.info("Iniciando extracción para %d puntos (Máx 20 simultáneos)...", len(tareas))
        resultados = await asyncio.gather(*tareas)
        
        # Limpiamos los None (calles que agotaron sus reintentos o tuvieron error fatal)
        resultados_validos = [r for r in resultados if r is not None]
        
        logger.info("Extracción finalizada: %d/%d vías obtenidas exitosamente.", len(resultados_validos), len(tareas))
        return resultados_validos
