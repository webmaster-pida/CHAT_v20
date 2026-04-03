# src/modules/perplexity_client.py
import httpx
from src.config import settings, log

async def get_perplexity_research(query: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Inyectamos una directriz de velocidad: No queremos un resumen extenso, solo datos y links.
    enhanced_query = f"Busca rápidamente datos concretos, hechos y URLs institucionales sobre: '{query}'. Sé muy conciso, no analices, solo entrega la información cruda y los enlaces."

    payload = {
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
    
    try:
        # Reducimos el timeout a 25 segundos para garantizar una buena experiencia de usuario
        timeout_config = httpx.Timeout(25.0, connect=10.0)
        
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status() 
            
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            citations = data.get("citations", [])
            
            # Formateamos las citas para que el regex de main.py las atrape fácilmente
            links_text = "\n\nFUENTES DE INTERNET:\n" + "\n".join(citations)
            return f"{content}\n{links_text}"
            
    except httpx.HTTPStatusError as e:
        log.error(f"Error HTTP de Perplexity ({e.response.status_code}): {e.response.text}", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: Error de autorización o de servidor en Perplexity]"
        
    except httpx.TimeoutException as e:
        log.error(f"Timeout: La API de Perplexity tardó demasiado en responder a la consulta.", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: El servidor de búsqueda superó el tiempo de espera]"
        
    except Exception as e:
        log.error(f"Error inesperado consultando Perplexity: {e}", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: Error de conexión desconocido]"
