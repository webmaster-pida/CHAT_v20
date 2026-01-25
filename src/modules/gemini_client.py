# src/modules/gemini_client.py

import vertexai
import asyncio 
import google.cloud.aiplatform as aiplatform
from vertexai.generative_models import GenerativeModel, Content, Part, GenerationConfig, Tool
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
    Incluye Grounding con Google Search usando inyección directa (from_dict).
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
        
        # --- SOLUCIÓN "KEYMASTER" PARA SDK 1.134.0 ---
        # El SDK tiene el método helper viejo ('from_google_search_retrieval'), 
        # pero la API de Gemini 2.5 exige el campo nuevo ('google_search').
        # Usamos 'from_dict' para construir manualmente el Tool correcto
        # saltándonos la lógica desactualizada del helper.
        
        google_search_tool = Tool.from_dict({
            "google_search": {} 
        })

        # Enviamos 'tools' como una lista de objetos Tool válidos.
        # Al haberlo creado con from_dict, el SDK lo reconoce como un objeto Tool legítimo.
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