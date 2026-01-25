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

def extract_domain(url: str) -> str:
    """Extrae el dominio base de una URL para comparar orígenes."""
    try:
        if not url: return ""
        netloc = urlparse(url).netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower()
    except:
        return ""

async def generate_streaming_response(system_prompt: str, prompt: str, history: List[Content]) -> AsyncGenerator[str, None]:
    """
    Genera respuesta con Google Search.
    1. Nutre la respuesta con información de internet.
    2. FILTRA el cuerpo del texto:
       - Links de RAG (tuyos) -> SE MANTIENEN.
       - Links de Google Search -> SE DESVINCULAN (Solo texto) para no alterar tu sección clásica.
    3. Agrega 'Fuentes Consultadas' al final con los links tal como vienen.
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        log.info(f"VERSIÓN SDK INSTALADA: {aiplatform.__version__}")

        # --- CORRECCIÓN AQUÍ: response_validation=False ---
        # Esto evita que el SDK lance una excepción si el modelo cita texto protegido (Recitation).
        # Permitimos que fluya el texto y dejamos que nuestros filtros de links hagan el resto.
        chat = model.start_chat(history=history, response_validation=False)
        
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        # 1. ACTIVAMOS GOOGLE SEARCH (Nutrición)
        google_search_tool = Tool.from_dict({
            "google_search": {} 
        })
        
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            tools=[google_search_tool]
        )

        # Variables para controlar fuentes
        google_domains_found = set()
        unique_footer_sources = {} 
        
        async for chunk in response_stream:
            
            # A. DETECTAR ORIGEN DE LA INFORMACIÓN
            if chunk.candidates and chunk.candidates[0].grounding_metadata:
                metadata = chunk.candidates[0].grounding_metadata
                if hasattr(metadata, 'grounding_chunks'):
                    for g_chunk in metadata.grounding_chunks:
                        if g_chunk.web and g_chunk.web.uri:
                            url = g_chunk.web.uri
                            title = g_chunk.web.title or "Fuente Web"
                            
                            # Registramos el dominio como "Fuente de Google"
                            domain = extract_domain(url)
                            if domain:
                                google_domains_found.add(domain)
                            
                            # Guardamos para el footer final tal cual viene
                            unique_footer_sources[url] = title

            # B. PROCESAMIENTO DEL TEXTO (El Filtro)
            try:
                if chunk.text:
                    text_content = chunk.text
                    
                    # Función que decide si un link se queda o se va
                    def filter_link_logic(match):
                        link_text = match.group(1) # El texto visible
                        link_url = match.group(2)  # La URL
                        
                        link_domain = extract_domain(link_url)

                        # ¿Es este link propiedad de Google Search?
                        is_google_link = False
                        
                        # Criterio 1: Es un link técnico de Vertex
                        if "vertexaisearch" in link_url or "google.com/grounding" in link_url:
                            is_google_link = True
                        
                        # Criterio 2: El dominio coincide con lo que Google trajo en esta búsqueda
                        if not is_google_link:
                            for g_domain in google_domains_found:
                                if g_domain and g_domain in link_domain:
                                    is_google_link = True
                                    break
                        
                        if is_google_link:
                            # ES DE GOOGLE -> Solo texto (Protegemos tu sección clásica)
                            return link_text
                        else:
                            # NO ES DE GOOGLE (Es tu RAG) -> Link completo
                            return match.group(0)

                    # Aplicamos el filtro a todos los links [Texto](URL)
                    pattern = r'\[([^\]]+)\]\((https?://[^\)]+)\)'
                    clean_text = re.sub(pattern, filter_link_logic, text_content)
                    
                    # Limpieza cosmética de citas numéricas [1]
                    clean_text = re.sub(r'\s?\[\d+\]', '', clean_text)
                    
                    yield clean_text
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