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
    # Esto desactiva completamente los filtros de seguridad de Vertex AI.
    # Es necesario para tratar casos crudos de violaciones a DDHH.
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
    """Convierte nuestro historial de Pydantic al formato que espera la API de Gemini."""
    vertex_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        vertex_history.append(Content(role=role, parts=[Part.from_text(message.content)]))
    return vertex_history

async def generate_streaming_response(
    system_prompt: str, 
    prompt: str, 
    history: List[Content],
    trusted_urls: Set[str] = set() # Lista blanca de URLs seguras (RAG)
) -> AsyncGenerator[str, None]:
    """
    Genera respuesta SIN CENSURA y con gestión inteligente de enlaces.
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        log.info(f"VERSIÓN SDK INSTALADA: {aiplatform.__version__}")

        # 1. INICIO DE CHAT PERMISIVO
        # response_validation=False: Evita error por Copyright/Citas textuales (Recitation)
        chat = model.start_chat(history=history, response_validation=False)
        
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        # 2. HERRAMIENTA DE BÚSQUEDA
        google_search_tool = Tool.from_dict({
            "google_search": {} 
        })
        
        # 3. GENERACIÓN DE CONTENIDO
        # Pasamos safety_settings con BLOCK_NONE para que no corte nada.
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            safety_settings=safety_settings, # <--- LA CLAVE DE LA NO-CENSURA
            tools=[google_search_tool]
        )

        unique_footer_sources = {} 
        
        async for chunk in response_stream:
            
            # A. RECOLECCIÓN DE METADATOS (Solo para el footer)
            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                metadata = chunk.candidates[0].grounding_metadata
                if hasattr(metadata, 'grounding_chunks'):
                    for g_chunk in metadata.grounding_chunks:
                        if g_chunk.web and g_chunk.web.uri:
                            url = g_chunk.web.uri
                            title = g_chunk.web.title or "Fuente Web"
                            unique_footer_sources[url] = title

            # B. PROCESAMIENTO DE TEXTO (LISTA BLANCA DE LINKS)
            try:
                if chunk.text:
                    text_content = chunk.text
                    
                    def filter_link_whitelist(match):
                        link_text = match.group(1) # Texto visible
                        link_url = match.group(2)  # URL
                        
                        # --- FILTRO DE LISTA BLANCA ---
                        # ¿Esta URL ya existía en tus documentos originales (trusted)?
                        is_trusted = False
                        clean_link = link_url.lower().strip()
                        
                        # Buscamos si la URL generada coincide con alguna de tus fuentes RAG
                        for t_url in trusted_urls:
                            t_clean = t_url.lower().strip()
                            if t_clean in clean_link or clean_link in t_clean:
                                is_trusted = True
                                break
                        
                        if is_trusted:
                            return match.group(0) # URL CONFIABLE -> LINK AZUL
                        else:
                            return link_text # URL NUEVA/GOOGLE -> TEXTO PLANO

                    # Aplicar filtro a todos los links Markdown [Texto](URL)
                    pattern = r'\[([^\]]+)\]\((https?://[^\)]+)\)'
                    clean_text = re.sub(pattern, filter_link_whitelist, text_content)
                    
                    # Limpieza estética de citas numéricas [1]
                    clean_text = re.sub(r'\s?\[\d+\]', '', clean_text)
                    
                    yield clean_text
            except Exception:
                continue

        # C. FOOTER "FUENTES CONSULTADAS" (Lo nuevo de Google)
        if unique_footer_sources:
            yield "\n\n---\n**Fuentes Consultadas:**\n"
            for url, title in unique_footer_sources.items():
                yield f"- [{title}]({url})\n"

    except Exception as e:
        log.error(f"Error en Gemini Client: {str(e)}", exc_info=True)
        # Mensaje de error genérico para el usuario
        yield "Hubo un problema al contactar al servicio de IA."