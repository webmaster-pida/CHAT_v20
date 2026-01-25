# src/modules/gemini_client.py

import vertexai
import asyncio 
import google.cloud.aiplatform as aiplatform
# IMPORTANTE: Importamos el módulo grounding explícitamente
from vertexai.generative_models import (
    GenerativeModel, 
    Content, 
    Part, 
    GenerationConfig, 
    Tool,
    grounding
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
    Incluye Grounding con Google Search usando Tipado Estricto (SDK 1.134.0).
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        # LOG DE VERIFICACIÓN
        log.info(f"VERSIÓN SDK INSTALADA: {aiplatform.__version__}")

        chat = model.start_chat(history=history)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        # --- SOLUCIÓN PARA SDK 1.134.0 ---
        # 1. Usamos el constructor de fábrica que devuelve un objeto Tool (no un dict).
        # 2. Pasamos el objeto de configuración desde el módulo grounding.
        # En la versión 1.134.0, se espera que este método mapee correctamente al campo 'google_search'.
        
        try:
            google_search_tool = Tool.from_google_search_retrieval(
                google_search_retrieval=grounding.GoogleSearchRetrieval()
            )
        except AttributeError:
            # Fallback de seguridad por si el método cambió de nombre en la versión final
            log.warning("Tool.from_google_search_retrieval falló, intentando Tool.from_google_search()")
            google_search_tool = Tool.from_google_search()

        # Enviamos 'tools' como una lista de objetos Tool válidos.
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