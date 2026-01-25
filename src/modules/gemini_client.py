# src/modules/gemini_client.py

import vertexai
import asyncio 
# CAMBIO IMPORTANTE: Usamos el namespace 'preview' para máxima compatibilidad con features recientes
from vertexai.preview.generative_models import (
    GenerativeModel, 
    Tool, 
    Content, 
    Part, 
    GenerationConfig,
    grounding # Importamos el módulo de grounding para el fallback
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
    Incluye Grounding con Google Search robusto.
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        # Iniciamos el chat (la sesión es local, no requiere await)
        chat = model.start_chat(history=history)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        # --- IMPLEMENTACIÓN ROBUSTA (A PRUEBA DE BALAS) ---
        # Intentamos primero el método moderno que exige la API (campo 'google_search').
        # Si el SDK de Python aún no lo tiene expuesto como helper, usamos el fallback manual.
        try:
            # Intento 1: La forma moderna 'from_google_search()'
            google_search_tool = Tool.from_google_search()
        except AttributeError:
            log.warning("Tool.from_google_search() no encontrado, usando fallback de grounding explícito.")
            # Intento 2: Estructura explícita usando el módulo grounding
            google_search_tool = Tool.from_google_search_retrieval(
                google_search_retrieval=grounding.GoogleSearchRetrieval()
            )
        
        # Enviamos el mensaje pasando la herramienta en una lista
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            tools=[google_search_tool]
        )

        # Iteramos sobre el generador asíncrono
        async for chunk in response_stream:
            if chunk.text:
                yield chunk.text

    except Exception as e:
        log.error(f"Error al generar la respuesta en streaming desde Gemini: {e}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."