# src/modules/gemini_client.py

import vertexai
import asyncio 
import re 
import random # Necesario para el "jitter" en la espera
import google.cloud.aiplatform as aiplatform
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, Aborted, InternalServerError # Excepciones de Google
from vertexai.generative_models import (
    GenerativeModel, 
    Content, 
    Part, 
    GenerationConfig, 
    Tool, 
    HarmCategory, 
    HarmBlockThreshold
)
from typing import List, AsyncGenerator, Set
from src.config import settings, log
from src.models.chat_models import ChatMessage

# --- INICIALIZACIÓN ---
try:
    vertexai.init(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_LOCATION)

    generation_config = GenerationConfig(
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
    )

    safety_settings = {
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    }

    model = GenerativeModel(settings.GEMINI_MODEL)
    log.info(f"Cliente de Vertex AI inicializado y modelo '{settings.GEMINI_MODEL}' cargado.")

except Exception as e:
    log.critical(f"No se pudo inicializar Vertex AI o cargar el modelo: {e}", exc_info=True)
    model = None

# --- UTILS ---

def prepare_history_for_vertex(history: List[ChatMessage]) -> List[Content]:
    vertex_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        vertex_history.append(Content(role=role, parts=[Part.from_text(message.content)]))
    return vertex_history

async def generate_streaming_response(
    system_prompt: str, 
    prompt: str, 
    history: List[Content],
    trusted_urls: Set[str] = set()
) -> AsyncGenerator[str, None]:
    
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    # CONFIGURACIÓN DE REINTENTOS (BACKOFF EXPONENCIAL)
    MAX_RETRIES = 3
    BASE_DELAY = 2 # Segundos iniciales de espera

    full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
    google_search_tool = Tool.from_dict({"google_search": {}})

    for attempt in range(MAX_RETRIES + 1):
        try:
            log.info(f"Intento de generación {attempt + 1}/{MAX_RETRIES + 1} con modelo {settings.GEMINI_MODEL}")
            
            # 1. INICIAR CHAT (Sin validación para evitar errores de Copyright)
            chat = model.start_chat(history=history, response_validation=False)
            
            # 2. ENVIAR MENSAJE
            response_stream = await chat.send_message_async(
                full_prompt, 
                stream=True, 
                generation_config=generation_config,
                safety_settings=safety_settings,
                tools=[google_search_tool]
            )

            # Si llegamos aquí, la conexión se estableció correctamente (superamos el 429 inicial)
            
            unique_footer_sources = {} 
            text_buffer = "" 

            # 3. PROCESAR STREAM
            async for chunk in response_stream:
                
                # A. METADATOS
                if chunk.candidates and chunk.candidates[0].grounding_metadata:
                    metadata = chunk.candidates[0].grounding_metadata
                    if hasattr(metadata, 'grounding_chunks'):
                        for g_chunk in metadata.grounding_chunks:
                            if g_chunk.web and g_chunk.web.uri:
                                url = g_chunk.web.uri
                                title = g_chunk.web.title or "Fuente Web"
                                unique_footer_sources[url] = title

                # B. LIMPIEZA DE TEXTO (Aggressive Cleaning + Whitelist)
                if chunk.text:
                    text_buffer += chunk.text
                    
                    # --- Lógica de Limpieza ---
                    def is_url_trusted(url_to_check):
                        clean_check = url_to_check.lower().strip().rstrip('/')
                        for t_url in trusted_urls:
                            clean_trust = t_url.lower().strip().rstrip('/')
                            if clean_check == clean_trust or clean_check.startswith(clean_trust):
                                return True
                        return False

                    # Links Markdown
                    md_pattern = r'\[([^\]]+)\]\s*\(\s*(https?://[^\s\)]+)\s*\)'
                    def replace_markdown_link(match):
                        return match.group(0) if is_url_trusted(match.group(2)) else match.group(1)
                    text_buffer = re.sub(md_pattern, replace_markdown_link, text_buffer)

                    # URLs Sueltas
                    raw_pattern = r'(?<!\()(https?://[^\s\)]+)' 
                    def replace_raw_url(match):
                        return match.group(0) if is_url_trusted(match.group(0)) else ""
                    text_buffer = re.sub(raw_pattern, replace_raw_url, text_buffer)
                    
                    # Artifacts de Citas [1]
                    text_buffer = re.sub(r'\s?\[\s*\d+\s*\]', '', text_buffer)

                    # AGGRESSIVE STRUCTURE CLEANING (Viñetas y bloques vacíos)
                    text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                    text_buffer = re.sub(r'(?m)^\s*>\s*>\s*$', '', text_buffer)
                    text_buffer = re.sub(r'\*\*\s*\*\*', '', text_buffer)
                    text_buffer = re.sub(r'\n\s*\n\s*\n', '\n\n', text_buffer)

                    # Buffer Streaming
                    if len(text_buffer) < 400: 
                        if any(text_buffer.strip().endswith(c) for c in ['[', '(', '*', '-', '>', '•']):
                            continue
                        yield text_buffer
                        text_buffer = ""
                    else:
                        yield text_buffer
                        text_buffer = ""

            # 4. VACIAR BUFFER FINAL (Éxito)
            if text_buffer:
                text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                yield text_buffer

            if unique_footer_sources:
                yield "\n\n---\n**Fuentes Consultadas:**\n"
                for url, title in unique_footer_sources.items():
                    yield f"- [{title}]({url})\n"
            
            # Si completamos el bucle sin error, salimos del retry
            return

        except (ResourceExhausted, ServiceUnavailable, Aborted, InternalServerError) as e:
            # --- LÓGICA DE REINTENTO ---
            log.warning(f"Error de saturación Vertex AI ({type(e).__name__}): {e}")
            
            if attempt < MAX_RETRIES:
                # Calculamos espera: Base * (2 ^ intento) + Jitter aleatorio
                # Ej: 2s -> 4s -> 8s (más unos milisegundos aleatorios para no colisionar)
                wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                log.info(f"Reintentando en {wait_time:.2f} segundos...")
                
                # Yield opcional para avisar al usuario (o mantenerlo en espera transparente)
                # yield f" [Red ocupada, reintentando en {int(wait_time)}s...] " 
                
                await asyncio.sleep(wait_time)
                continue # Vuelve al inicio del `for`
            else:
                # Se acabaron los intentos
                log.error("Se agotaron los reintentos de conexión con Vertex AI.")
                yield f"Error: El servicio de IA está saturado en este momento tras {MAX_RETRIES} intentos. Por favor, intenta de nuevo en un minuto."
                return

        except Exception as e:
            # Errores no recuperables (ej: Prompt bloqueado, error de código)
            log.error(f"Error irrecuperable en Gemini Client: {str(e)}", exc_info=True)
            yield "Hubo un problema inesperado al generar la respuesta."
            return