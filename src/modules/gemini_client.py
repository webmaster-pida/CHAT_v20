# src/modules/gemini_client.py

import vertexai
import asyncio
from vertexai.generative_models import GenerativeModel, Content, Part, GenerationConfig, Tool, GoogleSearchRetrieval
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

    # Inicializamos el modelo base.
    # Nota: La herramienta de grounding se pasará dinámicamente en la llamada de generación.
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
    Genera una respuesta del modelo Gemini utilizando Grounding con Google Search.
    Utiliza generate_content_async para pasar la herramienta y el historial completo.
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        # 1. Configuración de la herramienta de Grounding (Google Search)
        grounding_tool = Tool.from_google_search_retrieval(GoogleSearchRetrieval())

        # 2. Construcción del Prompt Completo
        # Combinamos el system_prompt con el prompt actual para asegurar que el modelo siga las instrucciones
        # junto con la capacidad de búsqueda.
        full_prompt_text = f"{system_prompt}\n\n---\n\n{prompt}"
        current_user_message = Content(role="user", parts=[Part.from_text(full_prompt_text)])
        
        # 3. Construcción del Historial Completo para la llamada (Historial previo + Mensaje actual)
        contents = history + [current_user_message]

        # 4. Llamada al modelo con la herramienta de Grounding
        response_stream = await model.generate_content_async(
            contents,
            tools=[grounding_tool],
            generation_config=generation_config,
            stream=True
        )

        # 5. Iteración sobre el stream de respuesta
        async for chunk in response_stream:
            if chunk.text:
                yield chunk.text
                # Cedemos el control al event loop para mantener la asincronía
                await asyncio.sleep(0)

    except Exception as e:
        log.error(f"Error al generar la respuesta en streaming con Grounding: {e}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."
