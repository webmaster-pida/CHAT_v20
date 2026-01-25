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

    # --- CONFIGURACIÓN "SIN CENSURA" (BLOCK_NONE) ---
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
    trusted_urls: Set[str] = set() # LISTA BLANCA
) -> AsyncGenerator[str, None]:
    
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        log.info(f"VERSIÓN SDK INSTALADA: {aiplatform.__version__}")

        # 1. INICIO DE CHAT
        chat = model.start_chat(history=history, response_validation=False)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        # 2. HERRAMIENTA DE BÚSQUEDA
        google_search_tool = Tool.from_dict({"google_search": {}})
        
        # 3. GENERACIÓN
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            safety_settings=safety_settings,
            tools=[google_search_tool]
        )

        unique_footer_sources = {} 
        
        async for chunk in response_stream:
            
            # A. RECOLECCIÓN DE METADATOS
            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                metadata = chunk.candidates[0].grounding_metadata
                if hasattr(metadata, 'grounding_chunks'):
                    for g_chunk in metadata.grounding_chunks:
                        if g_chunk.web and g_chunk.web.uri:
                            url = g_chunk.web.uri
                            title = g_chunk.web.title or "Fuente Web"
                            unique_footer_sources[url] = title

            # B. FILTRADO DE TEXTO (DOBLE PASADA)
            try:
                if chunk.text:
                    text_content = chunk.text
                    
                    # --- FUNCIÓN DE VALIDACIÓN ESTRICTA ---
                    def is_url_trusted(url_to_check):
                        clean_check = url_to_check.lower().strip().rstrip('/')
                        for t_url in trusted_urls:
                            # Comparamos la URL base limpia
                            clean_trust = t_url.lower().strip().rstrip('/')
                            # Si son idénticas o la de confianza es el inicio exacto de la generada
                            if clean_check == clean_trust or clean_check.startswith(clean_trust):
                                return True
                        return False

                    # 1. FILTRO MARKDOWN (Con tolerancia a espacios)
                    # Detecta: [Texto] ( URL )
                    def replace_markdown_link(match):
                        text = match.group(1)
                        url = match.group(2)
                        if is_url_trusted(url):
                            return match.group(0) # URL Segura -> Dejar link
                        return text # URL Nueva/Rota -> Solo texto

                    # Regex mejorada: \s* permite espacios entre corchetes y paréntesis
                    md_pattern = r'\[([^\]]+)\]\s*\(\s*(https?://[^\s\)]+)\s*\)'
                    text_content = re.sub(md_pattern, replace_markdown_link, text_content)

                    # 2. FILTRO DE URLS SUELTAS (Raw URLs)
                    # Detecta: https://... suelto en el texto
                    def replace_raw_url(match):
                        url = match.group(0)
                        if is_url_trusted(url):
                            return url
                        return "" # Si no es segura y está suelta, la borramos para que no se vea feo

                    # Regex para URLs que NO son parte de un link markdown (lookbehind negativo es complejo,
                    # así que simplificamos asumiendo que el paso 1 ya procesó los markdown).
                    # Esta regex busca http://... que haya quedado huérfano.
                    raw_pattern = r'(?<!\()(https?://[^\s\)]+)' 
                    text_content = re.sub(raw_pattern, replace_raw_url, text_content)
                    
                    # 3. LIMPIEZA FINAL
                    # Eliminar citas numéricas [1] y dobles espacios
                    text_content = re.sub(r'\s?\[\d+\]', '', text_content)
                    text_content = text_content.replace("  ", " ")
                    
                    yield text_content
            except Exception:
                continue

        # C. FOOTER "FUENTES CONSULTADAS"
        if unique_footer_sources:
            yield "\n\n---\n**Fuentes Consultadas:**\n"
            for url, title in unique_footer_sources.items():
                yield f"- [{title}]({url})\n"

    except Exception as e:
        log.error(f"Error en Gemini Client: {str(e)}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."