# src/modules/gemini_client.py

import vertexai
import asyncio 
import re # Importamos Regex para limpiar los links rotos
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
    Limpia enlaces rotos de vertexaisearch y añade fuentes reales al final.
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
        
        # 1. Configuración de la herramienta (Estrategia from_dict para SDK 1.134.0)
        # Usamos from_dict para inyectar 'google_search' sin depender de métodos que faltan en el SDK.
        google_search_tool = Tool.from_dict({
            "google_search": {} 
        })
        
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            tools=[google_search_tool]
        )

        # Variables para guardar las fuentes reales y evitar duplicados
        unique_sources = {} 
        
        async for chunk in response_stream:
            # A. Procesamiento de Metadatos (Extracción de URLs reales)
            # Verificamos si el chunk trae candidatos y metadatos de grounding
            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                metadata = chunk.candidates[0].grounding_metadata
                
                # Buscamos en los 'grounding_chunks' que es donde Gemini pone la data web
                if hasattr(metadata, 'grounding_chunks'):
                    for g_chunk in metadata.grounding_chunks:
                        # Si es un resultado web y tiene URL válida
                        if g_chunk.web and g_chunk.web.uri:
                            title = g_chunk.web.title or "Fuente Externa"
                            url = g_chunk.web.uri
                            # Guardamos en diccionario para evitar duplicados
                            unique_sources[url] = title

            # B. Procesamiento del Texto (Limpieza de Links Rotos)
            try:
                if chunk.text:
                    text_content = chunk.text
                    
                    # 1. ELIMINAR EL LINK ROTO (404)
                    # Patrón: [Texto](https://vertexaisearch...) -> **Texto**
                    clean_text = re.sub(
                        r'\[([^\]]+)\]\(https://vertexaisearch[^\)]+\)', 
                        r'**\1**', 
                        text_content
                    )
                    
                    # 2. Limpiar links sueltos sin corchetes si los hubiera
                    clean_text = re.sub(
                        r'https://vertexaisearch[^\s\)]+', 
                        '', 
                        clean_text
                    )

                    yield clean_text
            except Exception:
                # Si chunk.text falla (ej. bloqueado por seguridad), continuamos
                continue

        # C. AL FINAL: Agregamos las fuentes reales que sí funcionan
        if unique_sources:
            yield "\n\n**Fuentes Consultadas:**\n"
            for url, title in unique_sources.items():
                # Generamos una lista markdown con los enlaces originales (elpais, bbc, etc.)
                yield f"- [{title}]({url})\n"

    except Exception as e:
        log.error(f"FALLO CRÍTICO GEMINI 2.5: {str(e)}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."