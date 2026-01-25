# src/modules/gemini_client.py

import vertexai
import asyncio 
import re 
from urllib.parse import urlparse
import google.cloud.aiplatform as aiplatform
from vertexai.generative_models import GenerativeModel, Content, Part, GenerationConfig, Tool
from typing import List, AsyncGenerator
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
    model = GenerativeModel(settings.GEMINI_MODEL)
    log.info(f"Cliente de Vertex AI inicializado y modelo '{settings.GEMINI_MODEL}' cargado.")
except Exception as e:
    log.critical(f"No se pudo inicializar Vertex AI: {e}", exc_info=True)
    model = None

# --- UTILIDADES ---

def prepare_history_for_vertex(history: List[ChatMessage]) -> List[Content]:
    vertex_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        vertex_history.append(Content(role=role, parts=[Part.from_text(message.content)]))
    return vertex_history

def extract_domain(url: str) -> str:
    try:
        if not url: return ""
        netloc = urlparse(url).netloc
        if netloc.startswith("www."): netloc = netloc[4:]
        return netloc.lower()
    except: return ""

# --- GENERACIÓN ---

async def generate_streaming_response(system_prompt: str, prompt: str, history: List[Content]) -> AsyncGenerator[str, None]:
    """
    Genera respuesta combinando:
    1. Fuentes Clásicas (RAG + Vertex Search): Se mantienen intactas (links azules).
    2. Fuente Nueva (Google Tool): Nutre la respuesta, pero sus links se convierten a texto plano en el cuerpo.
    3. Footer: Muestra las fuentes de Google Tool al final.
    """
    if not model:
        yield "Error: Modelo no disponible."
        return

    try:
        # Iniciamos chat con el historial
        chat = model.start_chat(history=history)
        
        # El 'prompt' que recibimos YA CONTIENE la info de RAG y Vertex Search (ver src/main.py)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        # 1. Activamos la Herramienta Google Search (La nueva fuente)
        # Usamos from_dict para evitar errores de versión
        google_search_tool = Tool.from_dict({"google_search": {}})
        
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            tools=[google_search_tool]
        )

        # Rastreo de la "Fuente 3" (Google Tool)
        google_tool_domains = set()
        google_tool_sources = {} 
        
        async for chunk in response_stream:
            
            # A. Detectar qué viene de la Herramienta Google Search
            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                metadata = chunk.candidates[0].grounding_metadata
                if hasattr(metadata, 'grounding_chunks'):
                    for g_chunk in metadata.grounding_chunks:
                        if g_chunk.web and g_chunk.web.uri:
                            url = g_chunk.web.uri
                            title = g_chunk.web.title or "Fuente Web"
                            
                            # Registramos dominio para filtrarlo del texto
                            domain = extract_domain(url)
                            if domain: google_tool_domains.add(domain)
                            
                            # Guardamos para el footer
                            google_tool_sources[url] = title

            # B. Procesamiento del Texto
            try:
                if chunk.text:
                    text_content = chunk.text
                    
                    # LOGICA DEL FILTRO SELECTIVO:
                    # El objetivo es NO tocar los links que vienen de 'vertex_search_client.py' o 'rag_client.py'.
                    # Solo tocar los que Gemini inventa basándose en la herramienta nueva.

                    def filter_link_logic(match):
                        link_text = match.group(1) 
                        link_url = match.group(2)
                        link_domain = extract_domain(link_url)

                        # 1. Si es un link de redirección de Google -> TEXTO PLANO
                        if "vertexaisearch" in link_url or "google.com/grounding" in link_url:
                            return link_text
                        
                        # 2. Si el dominio coincide con lo que trajo la Herramienta Google Search -> TEXTO PLANO
                        # (Asumimos que estos son los propensos a 404 en el cuerpo)
                        is_from_google_tool = False
                        for g_domain in google_tool_domains:
                            if g_domain and g_domain in link_domain:
                                is_from_google_tool = True
                                break
                        
                        if is_from_google_tool:
                            return link_text
                        
                        # 3. Si no es nada de lo anterior, es de tus FUENTES ANTIGUAS -> DEJAR LINK
                        return match.group(0)

                    # Regex para encontrar links Markdown [Texto](URL)
                    pattern = r'\[([^\]]+)\]\((https?://[^\)]+)\)'
                    clean_text = re.sub(pattern, filter_link_logic, text_content)
                    
                    # Limpieza de citas numéricas [1]
                    clean_text = re.sub(r'\s?\[\d+\]', '', clean_text)
                    
                    yield clean_text
            except Exception:
                continue

        # C. Footer Solo para la Fuente Nueva
        if google_tool_sources:
            yield "\n\n---\n**Fuentes Consultadas (Adicionales):**\n"
            for url, title in google_tool_sources.items():
                yield f"- [{title}]({url})\n"

    except Exception as e:
        log.error(f"Error Gemini: {e}", exc_info=True)
        yield "Error al generar respuesta."