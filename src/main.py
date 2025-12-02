# /src/main.py

import json
import asyncio
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any

from src.config import settings, log
from src.models.chat_models import ChatRequest, ChatMessage
from src.modules import pse_client, gemini_client, rag_client, firestore_client
from src.core.prompts import PIDA_SYSTEM_PROMPT
from src.core.security import get_current_user

# --- INICIO DE LA MODIFICACIÓN ---
from google.cloud import firestore
# --- FIN DE LA MODIFICACIÓN ---

app = FastAPI(
    title="PIDA Backend API",
    description="API para el asistente jurídico PIDA, con persistencia en BD y autenticación."
)

# --- MODIFICACIÓN: Orígenes dinámicos desde config ---
try:
    origins = json.loads(settings.ALLOWED_ORIGINS)
except json.JSONDecodeError:
    log.error("Error al decodificar ALLOWED_ORIGINS. Usando fallback.")
    origins = ["https://pida.iiresodh.org", "https://pida-ai.com"]
# -----------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- INICIO DE LA MODIFICACIÓN: FUNCIÓN DE VERIFICACIÓN DE SUSCRIPCIÓN ACTUALIZADA ---
db = firestore.AsyncClient()

async def verify_active_subscription(current_user: Dict[str, Any]):
    """
    Verifica la suscripción de un usuario.
    Comprueba listas de acceso (dominios y emails) definidas en variables de entorno.
    """
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").lower()

    # --- LOGICA DE ACCESO DINÁMICA ---
    try:
        admin_domains = json.loads(settings.ADMIN_DOMAINS)
        admin_emails = json.loads(settings.ADMIN_EMAILS)
    except json.JSONDecodeError:
        log.error("Error decodificando listas de administración. Usando valores seguros.")
        admin_domains = []
        admin_emails = []

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    # 1. Bypass para el equipo interno (Dominios o Emails específicos)
    if (email_domain in admin_domains) or (user_email in admin_emails):
        log.info(f"Acceso de equipo concedido para el usuario {user_email}.")
        return
    # ---------------------------------

    # Verificación estándar para clientes
    try:
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        
        results = [doc async for doc in query.stream()]

        if not results:
            raise HTTPException(status_code=403, detail="No tienes una suscripción activa o un período de prueba para usar esta función.")
        
        log.info(f"Acceso de cliente verificado para el usuario {user_id}. Estado: {results[0].to_dict().get('status')}")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        log.error(f"Error al verificar la suscripción para el usuario {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Ocurrió un error al verificar tu estado de suscripción.")

# --- FIN DE LA MODIFICACIÓN ---


async def stream_chat_response_generator(chat_request: ChatRequest, country_code: str | None, user: Dict[str, Any], convo_id: str):
    user_id = user['uid']
    
    # --- INICIO DE LA MODIFICACIÓN: VERIFICACIÓN DENTRO DEL GENERADOR ---
    try:
        await verify_active_subscription(user) # Pasamos el objeto de usuario completo
    except HTTPException as e:
        yield f"data: {json.dumps({'error': e.detail})}\n\n"
        return
    # --- FIN DE LA MODIFICACIÓN ---
    
    def create_sse_event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        user_message = ChatMessage(role="user", content=chat_request.prompt)
        await firestore_client.add_message_to_conversation(user_id, convo_id, user_message)
        yield create_sse_event({"event": "status", "message": "Iniciando... 🕵️"})
        await asyncio.sleep(0.5)
        
        history_from_db = await firestore_client.get_conversation_messages(user_id, convo_id)
        history_for_gemini = gemini_client.prepare_history_for_vertex(history_from_db[:-1])
        
        yield create_sse_event({"event": "status", "message": "Consultando jurisprudencia y fuentes externas..."})
        search_tasks = [
            pse_client.search_for_sources(chat_request.prompt, num_results=3),
            rag_client.search_internal_documents(chat_request.prompt)
        ]
        combined_context = ""
        task_count = len(search_tasks)
        for i, task in enumerate(asyncio.as_completed(search_tasks)):
            result = await task
            combined_context += result
            yield create_sse_event({"event": "status", "message": f"Fuente de contexto ({i+1}/{task_count}) procesada..."})
        
        await asyncio.sleep(0.5)
        yield create_sse_event({"event": "status", "message": "Contexto recopilado. Construyendo la consulta..."})
        
        final_prompt = f"Contexto geográfico: {country_code}\n{combined_context}\n\n---\n\nPregunta del usuario: {chat_request.prompt}"
        
        yield create_sse_event({"event": "status", "message": f"Enviando a {settings.GEMINI_MODEL} para análisis... 🧠"})
        
        full_response_text = ""
        async for chunk in gemini_client.generate_streaming_response(
            system_prompt=PIDA_SYSTEM_PROMPT,
            prompt=final_prompt,
            history=history_for_gemini
        ):
            yield create_sse_event({'text': chunk})
            full_response_text += chunk

        if full_response_text:
            model_message = ChatMessage(role="model", content=full_response_text)
            await firestore_client.add_message_to_conversation(user_id, convo_id, model_message)

        log.info(f"Streaming finalizado para convo {convo_id}. Enviando evento 'done'.")
        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico durante el streaming para convo {convo_id}: {e}", exc_info=True)
        error_message = json.dumps({"error": "Lo siento, ocurrió un error interno al generar la respuesta."})
        yield f"data: {error_message}\n\n"


@app.get("/status", tags=["Status"])
def read_status():
    return {"status": "ok", "message": "PIDA Backend de Lógica funcionando."}

@app.get("/conversations", response_model=List[Dict[str, Any]], tags=["Chat History"])
async def get_user_conversations(current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user) # Modificado
    return await firestore_client.get_conversations(current_user['uid'])

@app.get("/conversations/{convo_id}/messages", response_model=List[ChatMessage], tags=["Chat History"])
async def get_conversation_details(convo_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user) # Modificado
    return await firestore_client.get_conversation_messages(current_user['uid'], convo_id)

@app.post("/conversations", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED, tags=["Chat History"])
async def create_new_empty_conversation(request: Request, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user) # Modificado
    body = await request.json()
    title = body.get("title", "Nuevo Chat")
    if not title:
        raise HTTPException(status_code=400, detail="El título no puede estar vacío")
    new_convo = await firestore_client.create_new_conversation(current_user['uid'], title)
    return new_convo

@app.delete("/conversations/{convo_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat History"])
async def delete_a_conversation(convo_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user) # Modificado
    await firestore_client.delete_conversation(current_user['uid'], convo_id)
    return

@app.patch("/conversations/{convo_id}/title", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat History"])
async def update_conversation_title_handler(
    convo_id: str, 
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    await verify_active_subscription(current_user) # Modificado
    body = await request.json()
    new_title = body.get("title")
    if not new_title:
        raise HTTPException(status_code=400, detail="El título no puede estar vacío")
    await firestore_client.update_conversation_title(current_user['uid'], convo_id, new_title)
    return

@app.post("/chat-stream/{convo_id}", tags=["Chat"])
async def chat_stream_handler(
    convo_id: str,
    chat_request: ChatRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    country_code = request.headers.get('X-Country-Code', None)
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no"
    }
    return StreamingResponse(
        stream_chat_response_generator(chat_request, country_code, current_user, convo_id),
        headers=headers
    )
