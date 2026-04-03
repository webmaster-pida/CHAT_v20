# src/modules/perplexity_client.py
import httpx
from src.config import settings, log

async def get_perplexity_research(query: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Inyectamos el requerimiento de investigación profunda
    enhanced_query = f"Investiga a fondo los antecedentes, jurisprudencia aplicable y hechos recientes sobre esta consulta: '{query}'. Proporciona un resumen detallado y extenso."

    payload = {
        "model": settings.PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system", 
                "content": """Eres un investigador legal y analista de élite del IIRESODH. Tu única tarea es buscar en la web información reciente, precisa y detallada sobre la consulta.
Reglas estrictas:
1. NO seas conversacional ni saludes.
2. IGNORA EL FORMATO: Si el usuario pide diseñar una tabla, carta, o cronograma, NO busques herramientas de diseño, software (Canva, Asana, Word, Excel) ni plantillas. Busca ÚNICAMENTE el contexto legal, fáctico, diplomático o de derechos humanos necesario para llenar ese formato.
3. Proporciona un resumen exhaustivo de los hechos, noticias, o jurisprudencia.
4. SIEMPRE incluye hipervínculos Markdown con las URLs completas de las fuentes dentro de tu texto (ejemplo: [Nombre del Sitio](https://...)).
5. Solo utiliza fuentes serias, institucionales, académicas o periodísticas."""
            },
            {"role": "user", "content": enhanced_query}
        ]
    }
    
    try:
        # AUMENTAMOS EL TIMEOUT A 60 SEGUNDOS. 
        # Modelos de razonamiento profundo como 'sonar-pro' requieren más tiempo de procesamiento.
        timeout_config = httpx.Timeout(60.0, connect=10.0)
        
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            response = await client.post(url, json=payload, headers=headers)
            
            # Lanzará una excepción si Perplexity devuelve un error (ej. 401 por API Key inválida)
            response.raise_for_status() 
            
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            citations = data.get("citations", [])
            
            # Formateamos las citas para que el regex de main.py las atrape fácilmente
            links_text = "\n\nFUENTES DE INTERNET:\n" + "\n".join(citations)
            return f"{content}\n{links_text}"
            
    except httpx.HTTPStatusError as e:
        # Esto capturará errores de la API (ej. problemas con la API Key, saldo agotado, o JSON mal formado)
        log.error(f"Error HTTP de Perplexity ({e.response.status_code}): {e.response.text}", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: Error de autorización o de servidor en Perplexity]"
        
    except httpx.TimeoutException as e:
        # Esto capturará si Perplexity tarda más de 60 segundos en responder
        log.error(f"Timeout: La API de Perplexity tardó demasiado en responder a la consulta.", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: El servidor de búsqueda superó el tiempo de espera]"
        
    except Exception as e:
        # Cualquier otro error de red o de código
        log.error(f"Error inesperado consultando Perplexity: {e}", exc_info=True)
        return "[INVESTIGACIÓN WEB FALLIDA: Error de conexión desconocido]"
