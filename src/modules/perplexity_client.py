# src/modules/perplexity_client.py
import httpx
from src.config import settings, log

async def get_perplexity_research(query: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": settings.PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system", 
                "content": """Eres un investigador legal y analista de datos de élite. Tu única tarea es buscar en la web la información más reciente, precisa y detallada sobre la consulta del usuario.
Reglas estrictas:
1. NO seas conversacional ni saludes. Ve directo a los datos.
2. Proporciona un resumen exhaustivo de los hechos, noticias, leyes o sentencias relevantes.
3. SIEMPRE incluye las URLs completas de las fuentes reales que consultaste para cada afirmación importante.
4. Si la consulta involucra datos numéricos o fechas recientes, asegúrate de confirmarlos en múltiples fuentes."""
            },
            {"role": "user", "content": query}
        ]
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"Error consultando Perplexity: {e}")
        return "No se pudo obtener información de internet en este momento."
