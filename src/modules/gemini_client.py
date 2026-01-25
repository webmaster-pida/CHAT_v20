# src/modules/gemini_client.py

import vertexai
import asyncio 
import re 
import google.cloud.aiplatform as aiplatform
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

# --- INICIALIZACIÓN DEL CLIENTE Y MODELO ---
try:
    vertexai.init(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_LOCATION)

    generation_config = GenerationConfig(
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
    )

    # CONFIGURACIÓN SIN CENSURA (BLOCK_NONE)
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

# --- FUNCIONES AUXILIARES ---

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

    try:
        log.info(f"VERSIÓN SDK INSTALADA: {aiplatform.__version__}")

        chat = model.start_chat(history=history, response_validation=False)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        google_search_tool = Tool.from_dict({"google_search": {}})
        
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            safety_settings=safety_settings,
            tools=[google_search_tool]
        )

        unique_footer_sources = {} 
        text_buffer = "" 

        async for chunk in response_stream:
            
            # A. METADATOS (Footer)
            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                metadata = chunk.candidates[0].grounding_metadata
                if hasattr(metadata, 'grounding_chunks'):
                    for g_chunk in metadata.grounding_chunks:
                        if g_chunk.web and g_chunk.web.uri:
                            url = g_chunk.web.uri
                            title = g_chunk.web.title or "Fuente Web"
                            unique_footer_sources[url] = title

            # B. PROCESAMIENTO Y LIMPIEZA
            try:
                if chunk.text:
                    text_buffer += chunk.text
                    
                    # --- 1. LÓGICA DE LISTA BLANCA ---
                    def is_url_trusted(url_to_check):
                        clean_check = url_to_check.lower().strip().rstrip('/')
                        for t_url in trusted_urls:
                            clean_trust = t_url.lower().strip().rstrip('/')
                            if clean_check == clean_trust or clean_check.startswith(clean_trust):
                                return True
                        return False

                    # --- 2. LIMPIEZA DE LINKS (MARKDOWN Y RAW) ---
                    # Markdown: [Texto](URL)
                    md_pattern = r'\[([^\]]+)\]\s*\(\s*(https?://[^\s\)]+)\s*\)'
                    def replace_markdown_link(match):
                        return match.group(0) if is_url_trusted(match.group(2)) else match.group(1)
                    text_buffer = re.sub(md_pattern, replace_markdown_link, text_buffer)

                    # URLs Sueltas: https://...
                    raw_pattern = r'(?<!\()(https?://[^\s\)]+)' 
                    def replace_raw_url(match):
                        return match.group(0) if is_url_trusted(match.group(0)) else ""
                    text_buffer = re.sub(raw_pattern, replace_raw_url, text_buffer)

                    # --- 3. LIMPIEZA DE VIÑETAS/BULLETS HUÉRFANOS (LO QUE SEÑALASTE) ---
                    # Elimina viñetas (•, *, -) que queden solas al final de una línea o bloque
                    # o que estén seguidas solo por espacios (típico cuando borramos el link que seguía).
                    text_buffer = re.sub(r'^\s*[•*\-]\s*$', '', text_buffer, flags=re.MULTILINE)
                    text_buffer = re.sub(r'\s+[•*\-]\s*$', '', text_buffer) 
                    
                    # Elimina viñetas que quedaron antes de un salto de línea doble
                    text_buffer = re.sub(r'\n\s*[•*\-]\s*\n', '\n', text_buffer)

                    # --- 4. LIMPIEZA DE ARTIFACTS DE CITAS ([1]) ---
                    text_buffer = re.sub(r'\s?\[\s*\d+\s*\]', '', text_buffer)

                    # --- 5. BUFFER STREAMING ---
                    # Retenemos el texto si termina en caracteres peligrosos que podrían ser el inicio de algo
                    if len(text_buffer) < 300: # Aumentamos buffer para atrapar bullets colgados
                        # Si termina en un posible inicio de viñeta o link, esperamos
                        if any(text_buffer.endswith(c) for c in ['[', '(', '•', '-', '*', ' ']):
                            continue
                        else:
                            yield text_buffer
                            text_buffer = ""
                    else:
                        yield text_buffer
                        text_buffer = ""

            except Exception:
                continue
        
        # 3. VACIAR BUFFER FINAL
        if text_buffer:
            # Última pasada agresiva para limpiar bullets finales
            text_buffer = re.sub(r'\s*[•*\-]\s*$', '', text_buffer)
            yield text_buffer

        # C. FOOTER
        if unique_footer_sources:
            yield "\n\n---\n**Fuentes Consultadas:**\n"
            for url, title in unique_footer_sources.items():
                yield f"- [{title}]({url})\n"

    except Exception as e:
        log.error(f"Error en Gemini Client: {str(e)}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."