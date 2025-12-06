# /src/main.py

import json
import asyncio
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any

from src.config import settings, log
from src.models.chat_models import ChatRequest, ChatMessage
# CORRECCIÓN: Imports limpios
from src.modules import vertex_search_client, gemini_client, rag_client, firestore_client
from src.core.prompts import PIDA_SYSTEM_PROMPT
from src.core.security import get_current_user

from google.cloud import firestore

app = FastAPI(
    title="PIDA Backend API",
    description="API para el asistente jurídico PIDA, con persistencia en BD y autenticación."
)

# --- CONFIGURACIÓN CORS ---
# Usamos la lista ya procesada y validada en config.py
origins = settings.ALLOWED_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CLIENTE FIRESTORE ASÍNCRONO ---
db = firestore.AsyncClient()

# --- FUNCIÓN DE VERIFICACIÓN DE SUSCRIPCIÓN ---
async def verify_active_subscription(current_user: Dict[str, Any]):
    """
    Verifica la suscripción de un usuario.
    Usa las listas optimizadas de settings.
    """
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").strip().lower()
    
    # Listas limpias desde configuración
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    # 1. Bypass para el equipo interno
    if (email_domain in admin_domains) or (user_email in admin_emails):
        log.info(f"Acceso VIP concedido en Chat: {user_email}")
        return
    # ---------------------------------

    # 2. Verificación estándar para clientes en Firestore
    try:
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        
        # Consumimos el stream asíncrono
        results = [doc async for doc in query.stream()]

        if not results:
            raise HTTPException(status_code=403, detail="No tienes una suscripción activa.")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        log.error(f"Error verificando suscripción DB: {e}")
        raise HTTPException(status_code=500, detail="Error interno verificando suscripción.")


# --- GENERADOR DE RESPUESTA STREAMING ---
async def stream_chat_response_generator(chat_request: ChatRequest, country_code: str | None, user: Dict[str, Any], convo_id: str):
    user_id = user['uid']
    
    # 1. Verificar Permisos
    try:
        await verify_active_subscription(user) 
    except HTTPException as e:
        yield f"data: {json.dumps({'error': e.detail})}\n\n"
        return
    
    def create_sse_event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        # 2. Guardar mensaje del usuario
        user_message = ChatMessage(role="user", content=chat_request.prompt)
        await firestore_client.add_message_to_conversation(user_id, convo_id, user_message)
        
        yield create_sse_event({"event": "status", "message": "Iniciando... 🕵️"})
        await asyncio.sleep(0.1) 
        
        # 3. Preparar Historial
        history_from_db = await firestore_client.get_conversation_messages(user_id, convo_id)
        history_for_gemini = gemini_client.prepare_history_for_vertex(history_from_db[:-1])
        
        # 4. BÚSQUEDA PARALELA (Vertex AI + RAG Interno)
        yield create_sse_event({"event": "status", "message": "Consultando jurisprudencia (Vertex AI) y documentos internos..."})
        
        search_tasks = [
            asyncio.to_thread(vertex_search_client.search, chat_request.prompt, num_results=3),
            rag_client.search_internal_documents(chat_request.prompt)
        ]
        
        combined_context = ""
        
        for i, task in enumerate(asyncio.as_completed(search_tasks)):
            result = await task
            combined_context += result
            yield create_sse_event({"event": "status", "message": f"Fuente {i+1} procesada..."})
        
        yield create_sse_event({"event": "status", "message": "Analizando información..."})
        
        # 5. Construir Prompt Final
        final_prompt = f"Contexto geográfico: {country_code}\n{combined_context}\n\n---\n\nPregunta del usuario: {chat_request.prompt}"
        
        yield create_sse_event({"event": "status", "message": f"Generando respuesta... 🧠"})
        
        # 6. Generar respuesta con Gemini (AHORA ASÍNCRONO REAL)
        full_response_text = ""
        async for chunk in gemini_client.generate_streaming_response(
            system_prompt=PIDA_SYSTEM_PROMPT,
            prompt=final_prompt,
            history=history_for_gemini
        ):
            yield create_sse_event({'text': chunk})
            full_response_text += chunk

        # 7. Guardar respuesta del modelo
        if full_response_text:
            model_message = ChatMessage(role="model", content=full_response_text)
            await firestore_client.add_message_to_conversation(user_id, convo_id, model_message)

        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico streaming convo {convo_id}: {e}", exc_info=True)
        error_message = json.dumps({"error": "Ocurrió un error interno al generar la respuesta."})
        yield f"data: {error_message}\n\n"


# --- ENDPOINTS ---

@app.get("/status", tags=["Status"])
def read_status():
    return {"status": "ok", "message": "PIDA Backend v2.0 (Vertex AI) Online."}

@app.get("/conversations", response_model=List[Dict[str, Any]], tags=["Chat History"])
async def get_user_conversations(current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user)
    return await firestore_client.get_conversations(current_user['uid'])

@app.get("/conversations/{convo_id}/messages", response_model=List[ChatMessage], tags=["Chat History"])
async def get_conversation_details(convo_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user)
    return await firestore_client.get_conversation_messages(current_user['uid'], convo_id)

@app.post("/conversations", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED, tags=["Chat History"])
async def create_new_empty_conversation(request: Request, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user)
    body = await request.json()
    title = body.get("title", "Nuevo Chat")
    if not title:
        raise HTTPException(status_code=400, detail="El título no puede estar vacío")
    new_convo = await firestore_client.create_new_conversation(current_user['uid'], title)
    return new_convo

@app.delete("/conversations/{convo_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat History"])
async def delete_a_conversation(convo_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user)
    await firestore_client.delete_conversation(current_user['uid'], convo_id)
    return

@app.patch("/conversations/{convo_id}/title", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat History"])
async def update_conversation_title_handler(
    convo_id: str, 
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    await verify_active_subscription(current_user)
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
