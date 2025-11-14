# /src/main.py

import json
import asyncio
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any

from src.config import settings, log
from src.models.chat_models import ChatRequest, ChatMessage
# SE ELIMINA pse_client de la importación
from src.modules import gemini_client, rag_client, firestore_client
from src.core.prompts import PIDA_SYSTEM_PROMPT
from src.core.security import get_current_user

from google.cloud import firestore

app = FastAPI(
    title="PIDA Backend API",
    description="API para el asistente jurídico PIDA, con persistencia en BD y autenticación."
)

origins = [
    "https://pida.iiresodh.org",
    "https://pida-ai.com",
    "http://localhost",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = firestore.AsyncClient()

async def verify_active_subscription(current_user: Dict[str, Any]):
    """
    Verifica la suscripción de un usuario.
    """
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").lower()

    # Bypass para el equipo interno
    if user_email.endswith("@iiresodh.org") or user_email.endswith("@urquilla.com"):
        log.info(f"Acceso de equipo concedido para el usuario {user_email}.")
        return

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


async def stream_chat_response_generator(chat_request: ChatRequest, country_code: str | None, user: Dict[str, Any], convo_id: str):
    user_id = user['uid']
    
    try:
        await verify_active_subscription(user)
    except HTTPException as e:
        yield f"data: {json.dumps({'error': e.detail})}\n\n"
        return
    
    def create_sse_event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        user_message = ChatMessage(role="user", content=chat_request.prompt)
        await firestore_client.add_message_to_conversation(user_id, convo_id, user_message)
        yield create_sse_event({"event": "status", "message": "Iniciando... 🕵️"})
        await asyncio.sleep(0.5)
        
        history_from_db = await firestore_client.get_conversation_messages(user_id, convo_id)
        history_for_gemini = gemini_client.prepare_history_for_vertex(history_from_db[:-1])
        
        # Se elimina la lógica de búsqueda manual (PSE y RAG) para confiar en el Grounding nativo y el prompt directo.
        yield create_sse_event({"event": "status", "message": "Analizando consulta y contexto geográfico..."})
        
        # Construcción simplificada del prompt final
        final_prompt = f"Contexto geográfico: {country_code}\n\nPregunta del usuario: {chat_request.prompt}"
        
        yield create_sse_event({"event": "status", "message": f"Enviando a {settings.GEMINI_MODEL} con búsqueda conectada... 🧠"})
        
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
