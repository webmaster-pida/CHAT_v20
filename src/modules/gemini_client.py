# src/modules/gemini_client.py

import vertexai
import asyncio 
import google.cloud.aiplatform as aiplatform
from vertexai.generative_models import (
    GenerativeModel, 
    Content, 
    Part, 
    GenerationConfig, 
    Tool,
    GoogleSearch # <--- Clase correcta para Gemini 2.5 Pro en SDK 1.134.0
)
from typing import List, AsyncGenerator
from src.config import settings, log
from src.models.chat_models import ChatMessage

# --- INICIALIZACIÓN DEL CLIENTE Y MODELO ---
try:
    vertexai.init(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_LOCATION)

    generation_config = GenerationConfig(
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
    )

    model = GenerativeModel(settings.GEMINI_MODEL)
    log.info(f"Cliente de Vertex AI inicializado y modelo '{settings.GEMINI_MODEL}' cargado.")

except Exception as e:
    log.critical(f"No se pudo inicializar Vertex AI o cargar el modelo: {e}", exc_info=True)
    model = None

# --- FUNCIONES AUXILIARES ---

def prepare_history_for_vertex(history: List[ChatMessage]) -> List[Content]:
    """Convierte nuestro historial de Pydantic al formato que espera la API de Gemini."""
    vertex_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        vertex_history.append(Content(role=role, parts=[Part.from_text(message.content)]))
    return vertex_history

async def generate_streaming_response(system_prompt: str, prompt: str, history: List[Content]) -> AsyncGenerator[str, None]:
    """
    Genera una respuesta del modelo Gemini en modo streaming ASÍNCRONO REAL.
    Usa send_message_async para no bloquear el event loop.
    Incluye Grounding con Google Search para Gemini 2.5 Pro (SDK 1.134.0).
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        # LOG de verificación para asegurarnos que Cloud Run cargó la versión correcta
        log.info(f"SDK Version: {aiplatform.__version__}")

        # --- SOLUCIÓN PARA GEMINI 2.5 PRO (ENERO 2026) ---
        google_search_tool = None
        
        try:
            # Intento 1: Método de fábrica con la clase GoogleSearch
            # Este método debería generar el JSON {"google_search": {}}
            google_search_tool = Tool.from_google_search(
                google_search=GoogleSearch()
            )
        except AttributeError:
            # Intento 2: Constructor directo (Fallback)
            # Si 'from_google_search' no existe, usamos el constructor base
            # pasando el objeto GoogleSearch al campo 'google_search'.
            log.warning("Método 'from_google_search' no encontrado, usando constructor directo Tool(google_search=...).")
            google_search_tool = Tool(
                google_search=GoogleSearch()
            )

        chat = model.start_chat(history=history)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            tools=[google_search_tool] 
        )

        async for chunk in response_stream:
            if chunk.text:
                yield chunk.text

    except Exception as e:
        log.error(f"FALLO CRÍTICO GEMINI 2.5: {str(e)}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."