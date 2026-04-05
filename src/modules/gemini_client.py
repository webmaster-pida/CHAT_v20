# src/modules/gemini_client.py

import vertexai
import asyncio 
import re 
import random 
import google.cloud.aiplatform as aiplatform
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, Aborted, InternalServerError
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

    # Mantenemos este modelo global para verificar que la inicialización general funciona
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

    # RETRY LOGIC
    MAX_RETRIES = 3
    BASE_DELAY = 2 

    # 👇 CORRECCIÓN: Instanciamos el modelo usando system_instruction nativo
    # Esto evita contaminar el prompt del usuario y mantiene la "memoria" intacta.
    model_with_system = GenerativeModel(
        settings.GEMINI_MODEL,
        system_instruction=[system_prompt]
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            # Usamos el modelo instanciado con el system prompt
            chat = model_with_system.start_chat(history=history, response_validation=False)
            
            # Enviamos ÚNICAMENTE el prompt del turno actual (ya trae Perplexity y RAG)
            response_stream = await chat.send_message_async(
                prompt, 
                stream=True, 
                generation_config=generation_config,
                safety_settings=safety_settings
            )

            text_buffer = "" 

            async for chunk in response_stream:
                if chunk.text:
                    text_buffer += chunk.text
                    
                    # --- CLEANING LOGIC MEJORADO ---
                    
                    # 1. SE ELIMINÓ EL FILTRO ESTRICTO DE URLs QUE BORRABA LOS ENLACES.
                    # Ahora confiamos en las instrucciones del System Prompt para evitar alucinaciones,
                    # permitiendo que los enlaces Markdown pasen intactos hacia las tarjetas de la UI.

                    # 2. Limpieza de Artifacts de Citas de Perplexity: [1], (2), [3, 4]
                    # Eliminamos las citas numéricas residuales que ensucian el texto y confundían a Gemini
                    text_buffer = re.sub(r'\s?[\[\(]\s*\d+(?:\s*,\s*\d+)*\s*[\]\)]', '', text_buffer)

                    # 3. REPARACIÓN DE MARKDOWN ROTO
                    text_buffer = text_buffer.replace(">**", "**")
                    text_buffer = text_buffer.replace(" <", " \"")
                    text_buffer = text_buffer.replace("> ", "\" ")
                    text_buffer = re.sub(r'\*\*\s*$', '', text_buffer, flags=re.MULTILINE)

                    # 4. AGGRESSIVE STRUCTURE CLEANING
                    text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                    text_buffer = re.sub(r'(?m)^\s*>\s*>\s*$', '', text_buffer)
                    text_buffer = re.sub(r'\n\s*\n\s*\n', '\n\n', text_buffer)

                    # 5. BUFFERING INTELIGENTE PARA NO ROMPER ENLACES MARKDOWN
                    if len(text_buffer) < 400: 
                        if any(text_buffer.strip().endswith(c) for c in ['[', '(', '*', '-', '>', '•', 'http', 'https']):
                            continue
                        yield text_buffer
                        text_buffer = ""
                    else:
                        # Protección anti-ruptura: No cortar el buffer si hay un corchete o paréntesis de enlace abierto
                        if text_buffer.count('[') > text_buffer.count(']') or text_buffer.count('(') > text_buffer.count(')'):
                            continue
                        
                        yield text_buffer
                        text_buffer = ""

            if text_buffer:
                text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                yield text_buffer
            
            return 

        except (ResourceExhausted, ServiceUnavailable, Aborted, InternalServerError) as e:
            log.warning(f"Vertex AI Error ({type(e).__name__}): {e} - Reintentando...")
            if attempt < MAX_RETRIES:
                wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                await asyncio.sleep(wait_time)
                continue
            else:
                log.error("Agotados reintentos Vertex AI.")
                yield f"Error: El sistema está saturado. Intente de nuevo más tarde."
                return

        except Exception as e:
            log.error(f"Error Gemini: {e}", exc_info=True)
            yield "Error inesperado en la generación."
            return
