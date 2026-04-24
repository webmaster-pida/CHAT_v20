# src/modules/gemini_client.py

import asyncio 
import re 
import random 
from typing import List, AsyncGenerator, Set

# Nuevas importaciones del SDK de GenAI
from google import genai
from google.genai import types
from google.genai.errors import APIError

from src.config import settings, log
from src.models.chat_models import ChatMessage

# --- INICIALIZACIÓN ---
try:
    # El nuevo cliente maneja la inicialización internamente
    client = genai.Client(
        vertexai=True, 
        project=settings.GOOGLE_CLOUD_PROJECT, 
        location=settings.GOOGLE_CLOUD_LOCATION
    )
    log.info(f"Cliente de GenAI inicializado y apuntando al modelo '{settings.GEMINI_MODEL}'.")

except Exception as e:
    log.critical(f"No se pudo inicializar GenAI Client: {e}", exc_info=True)
    client = None

# --- UTILS ---

def prepare_history_for_genai(history: List[ChatMessage]) -> List[types.Content]:
    """Convierte el historial de BD al nuevo formato Content del SDK google-genai"""
    genai_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        genai_history.append(
            types.Content(
                role=role, 
                parts=[types.Part.from_text(text=message.content)]
            )
        )
    return genai_history

async def generate_streaming_response(
    system_prompt: str, 
    prompt: str, 
    history: List[types.Content],
    trusted_urls: Set[str] = set()
) -> AsyncGenerator[str, None]:
    
    if not client:
        log.error("El modelo Gemini no está disponible (Cliente no inicializado).")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    # RETRY LOGIC
    MAX_RETRIES = 3
    BASE_DELAY = 2 

    # 👇 NUEVA CONFIGURACIÓN: El System Prompt y la Seguridad ahora van agrupados aquí
    generation_config = types.GenerateContentConfig(
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
        system_instruction=system_prompt,
        safety_settings=[
            types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
            types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
            types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
            types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
        ]
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            # 👇 NUEVO CHAT ASÍNCRONO: Instanciamos el chat con la configuración y el historial previo
            chat = client.aio.chats.create(
                model=settings.GEMINI_MODEL,
                config=generation_config,
                history=history
            )
            
            # Enviamos ÚNICAMENTE el prompt del turno actual (ya trae Perplexity y RAG)
            response_stream = await chat.send_message_stream(prompt)

            text_buffer = "" 

            async for chunk in response_stream:
                if chunk.text:
                    text_buffer += chunk.text
                    
                    # --- CLEANING LOGIC (Se mantiene tu lógica exacta anti-alucinaciones y formato) ---
                    text_buffer = re.sub(r'\s?[\[\(]\s*\d+(?:\s*,\s*\d+)*\s*[\]\)]', '', text_buffer)
                    text_buffer = text_buffer.replace(">**", "**")
                    text_buffer = text_buffer.replace(" <", " \"")
                    text_buffer = text_buffer.replace("> ", "\" ")
                    text_buffer = re.sub(r'\*\*\s*$', '', text_buffer, flags=re.MULTILINE)
                    text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                    text_buffer = re.sub(r'(?m)^\s*>\s*>\s*$', '', text_buffer)
                    text_buffer = re.sub(r'\n\s*\n\s*\n', '\n\n', text_buffer)

                    # BUFFERING INTELIGENTE PARA NO ROMPER ENLACES MARKDOWN
                    if len(text_buffer) < 50: 
                        if any(text_buffer.strip().endswith(c) for c in ['[', '(', '*', '-', '>', '•', 'http', 'https']):
                            continue
                        yield text_buffer
                        text_buffer = ""
                    else:
                        if text_buffer.count('[') > text_buffer.count(']') or text_buffer.count('(') > text_buffer.count(')'):
                            continue
                        
                        yield text_buffer
                        text_buffer = ""

            if text_buffer:
                text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                yield text_buffer
            
            return 

        except APIError as e:
            # Nuevo manejo de errores de cuotas o servidores caídos basados en códigos HTTP
            if e.code in [429, 503, 500]:
                log.warning(f"GenAI Error de cuota/servidor ({e.code}): {e.message} - Reintentando...")
                if attempt < MAX_RETRIES:
                    wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    log.error("Agotados reintentos del cliente GenAI.")
                    yield f"Error: El sistema de IA está saturado. Intente de nuevo más tarde."
                    return
            else:
                log.error(f"Error crítico en GenAI: {e}", exc_info=True)
                yield "Error inesperado en la generación (Código no recuperable)."
                return

        except Exception as e:
            log.error(f"Error inesperado genérico de Python: {e}", exc_info=True)
            yield "Error interno al procesar la respuesta."
            return
