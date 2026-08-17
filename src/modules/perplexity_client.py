# src/modules/perplexity_client.py
import httpx
from src.config import settings, log

# 1. CREAMOS EL CLIENTE GLOBAL (Persistente)
# Esto mantiene el "pool" de conexiones abierto.
_http_client = None

def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        # Configuramos los timeouts (ajustables)
        timeout_config = httpx.Timeout(25.0, connect=10.0)
        
        # Opcional pero recomendado: Limitar conexiones simultáneas para no saturar 
        # tu propio servidor si hay muchos usuarios al mismo tiempo.
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        
        _http_client = httpx.AsyncClient(
            timeout=timeout_config,
            limits=limits,
            # Añadimos los headers estáticos aquí para no enviarlos en cada método
            headers={
                "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
                "Content-Type": "application/json"
            }
        )
    return _http_client

# Opcional: Función para cerrar el cliente (útil si manejas un apagado elegante en FastAPI)
async def close_http_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


async def get_perplexity_research(query: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    
    enhanced_query = f"Busca rápidamente datos concretos, hechos y URLs institucionales sobre: '{query}'. Sé muy conciso, no analices, solo entrega la información cruda y los enlaces."

    payload = {
        # 2. ASEGÚRATE DE USAR EL MODELO RÁPIDO
        # Verifica que settings.PERPLEXITY_MODEL sea 'sonar-small-online' o el más ligero.
        "model": settings.PERPLEXITY_MODEL, 
        "messages": [
            {
                "role": "system", 
                "content": """Eres un buscador rápido de datos para el IIRESODH. Tu única tarea es encontrar información factual y URLs reales.
Reglas estrictas:
1. NO seas conversacional ni redactes introducciones o conclusiones.
2. Entrega ÚNICAMENTE párrafos cortos con los hechos, datos o noticias encontradas.
3. SIEMPRE incluye hipervínculos Markdown con las URLs completas (ejemplo: [ONU](https://...)).
4. Prioriza la VELOCIDAD y la precisión de los enlaces por encima de la longitud de texto. Solo usa fuentes serias."""
            },
            {"role": "user", "content": enhanced_query}
        ]
    }
    
    # 3. USAMOS EL CLIENTE GLOBAL
    client = get_http_client()
    
    try:
        # Ya no usamos 'async with', solo hacemos la petición directamente
        response = await client.post(url, json=payload)
        response.raise_for_status() 
        
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])
        
        links_text = "\n\nFUENTES DE INTERNET:\n" + "\n".join(citations)
        return f"{content}\n{links_text}"
        
    except httpx.HTTPStatusError as e:
        log.error(f"Error HTTP de Perplexity ({e.response.status_code}): {e.response.text}", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: Error de autorización o de servidor en Perplexity]"
        
    except httpx.TimeoutException as e:
        log.error(f"Timeout: La API de Perplexity tardó demasiado.", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: El servidor de búsqueda superó el tiempo de espera]"
        
    except Exception as e:
        log.error(f"Error inesperado consultando Perplexity: {e}", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: Error de conexión desconocido]"
