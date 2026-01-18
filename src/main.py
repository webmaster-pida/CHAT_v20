# /src/main.py

import os
import stripe
from firebase_admin import auth as firebase_auth
import json
import asyncio
import io
import re
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any

# Librerías para documentos
from docx import Document
from fpdf import FPDF

from src.config import settings, log
from src.models.chat_models import ChatRequest, ChatMessage
from src.modules import vertex_search_client, gemini_client, rag_client, firestore_client
from src.core.prompts import PIDA_SYSTEM_PROMPT
from src.core.security import get_current_user

from google.cloud import firestore

# MAPA DE TRADUCCIÓN: ID de Stripe -> Nombre del Plan interno
STRIPE_PRICE_MAP = {
    # BÁSICO
    "price_1SqbBOGaDEQrzamxiuEbXIcc": "basico", # USD Mensual
    "price_1SqFSFGgaloBN5L8BMBeRPqb": "basico", # MXN Mensual
    "price_1SqFSFGgaloBN5L8kxegWZqC": "basico", # USD Anual
    "price_1SqFSyGgaloBN5L8rrwrtUau": "basico", # MXN Anual
    
    # AVANZADO
    "price_1SqbD8GaDEQrzamxuV9SQbFB": "avanzado", # USD Mensual
    "price_1SqFWJGgaloBN5L8roECNay2": "avanzado", # MXN Mensual
    "price_1SqFWJGgaloBN5L8VKhkzLRH": "avanzado", # USD Anual
    "price_1SqFWJGgaloBN5L8hKpEvd1v": "avanzado", # MXN Anual

    # PREMIUM
    "price_1SqbDcGaDEQrzamxdcvIy0BG": "premium", # USD Mensual
    "price_1SqFadGgaloBN5L8AwTUeTSd": "premium", # MXN Mensual
    "price_1SqFadGgaloBN5L86iwNYm1c": "premium", # USD Anual
    "price_1SqFadGgaloBN5L8QFHXe1i9": "premium", # MXN Anual
}

# --- LÍMITES DE CHAT (Definidos en src/config.py) ---
CHAT_LIMITS = {
    "demo": settings.LIMIT_DEMO_CHAT_DAILY,
    "basico": settings.LIMIT_BASICO_CHAT_DAILY,
    "avanzado": settings.LIMIT_AVANZADO_CHAT_DAILY,
    "premium": settings.LIMIT_PREMIUM_CHAT_DAILY,
    "vip": -1
}

# Inicializar Stripe con la llave secreta
stripe.api_key = settings.STRIPE_SECRET_KEY

app = FastAPI(
    title="PIDA Backend API",
    description="API para el asistente jurídico PIDA, con persistencia en BD y autenticación."
)

# --- CONFIGURACIÓN CORS ---
origins = settings.ALLOWED_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    # Modificado para aceptar Firebase Y Codespaces dinámicos
    allow_origin_regex=r"https://pida-ai-v20--.*\.web\.app$|https://.*\.app\.github\.dev$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CLIENTE FIRESTORE ASÍNCRONO ---
db = firestore.AsyncClient()

# --- UTILIDADES DE NOMBRE DE ARCHIVO Y TEXTO (IGUAL QUE ANALIZADOR) ---
def generate_filename(title: str, extension: str) -> str:
    """Genera nombre seguro con fecha: Titulo_YYYY-MM-DD-HH-MM-SS.ext"""
    safe_title = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ]', '', title[:40])
    safe_title = safe_title.strip().replace(' ', '_')
    if not safe_title:
        safe_title = "Chat_PIDA"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return f"{safe_title}_{timestamp}.{extension}"

def sanitize_text_for_pdf(text: str) -> str:
    """Limpia caracteres incompatibles con Latin-1."""
    if not text: return ""
    replacements = {
        "•": "-", "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
        "\u2013": "-", "\u2014": "-", "\u2022": "-", "\uF0B7": "-"
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text.encode('latin1', 'replace').decode('latin-1')

def write_markdown_to_pdf(pdf, text):
    """Interpreta Markdown básico para el PDF."""
    pdf.set_font("Arial", "", 11)
    
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            pdf.ln(5)
            continue
            
        # Detectar roles de chat (ej: **Usuario**:)
        if line.startswith('**') and ':**' in line:
            parts = line.split(':**', 1)
            role = parts[0].replace('**', '')
            content = parts[1].strip()
            
            # Escribir Rol en Negrita y Color
            pdf.set_font("Arial", "B", 11)
            pdf.set_text_color(29, 53, 87) # Navy PIDA
            pdf.write(6, f"{role}: ")
            
            # Escribir contenido normal
            pdf.set_font("Arial", "", 11)
            pdf.set_text_color(0, 0, 0)
            
            # Procesar negritas internas en el contenido
            sub_parts = re.split(r'(\*\*.*?\*\*)', content)
            for sp in sub_parts:
                if sp.startswith('**') and sp.endswith('**'):
                    pdf.set_font("Arial", "B", 11)
                    pdf.write(6, sp.strip('*'))
                    pdf.set_font("Arial", "", 11)
                else:
                    pdf.write(6, sp)
            pdf.ln(6)
            
        # Títulos
        elif line.startswith('## '):
            pdf.ln(3)
            pdf.set_font("Arial", "B", 13)
            pdf.set_text_color(29, 53, 87)
            pdf.multi_cell(0, 8, line.replace('## ', ''))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Arial", "", 11)
            
        # Listas
        elif line.startswith('* ') or line.startswith('- '):
            pdf.set_x(15)
            pdf.write(6, "- " + line[2:])
            pdf.ln(6)
            
        # Texto normal
        else:
            pdf.multi_cell(0, 6, line)

# --- CLASE PDF ---
class PDF(FPDF):
    def header(self):
        self.set_font("Arial", "B", 14)
        self.set_text_color(29, 53, 87)
        self.cell(0, 10, "PIDA-AI: Historial de Chat", 0, 1, "L")
        self.set_font("Arial", "", 9)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Generado: {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}", 0, 1, "L")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", 0, 0, "C")

# --- GENERADORES SÍNCRONOS ---
def create_chat_docx_sync(chat_text: str, title: str) -> tuple[bytes, str, str]:
    stream = io.BytesIO()
    doc = Document()
    doc.add_heading("PIDA-AI: Historial de Chat", 0)
    doc.add_paragraph(f"Tema: {title}")
    doc.add_paragraph(f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    doc.add_heading("Conversación", 1)
    
    for line in chat_text.split('\n'):
        if line.strip():
            doc.add_paragraph(line)
            
    doc.save(stream)
    stream.seek(0)
    fname = generate_filename(title, "docx")
    return stream.read(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", fname

def create_chat_pdf_sync(chat_text: str, title: str) -> tuple[bytes, str, str]:
    safe_text = sanitize_text_for_pdf(chat_text)
    safe_title = sanitize_text_for_pdf(title)
    
    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    
    # Título del Chat
    pdf.set_font("Arial", "B", 12)
    pdf.multi_cell(0, 6, f"Tema: {safe_title}")
    pdf.ln(5)
    
    # Contenido
    if not safe_text.strip():
        pdf.multi_cell(0, 6, "[Chat vacío]")
    else:
        write_markdown_to_pdf(pdf, safe_text)
        
    try:
        pdf_string = pdf.output(dest='S')
        if isinstance(pdf_string, str):
            pdf_bytes = pdf_string.encode('latin-1', 'replace')
        else:
            pdf_bytes = pdf_string
        stream = io.BytesIO(pdf_bytes)
        fname = generate_filename(title, "pdf")
        return stream.read(), "application/pdf", fname
    except Exception as e:
        print(f"Error PDF: {e}")
        err = FPDF()
        err.add_page()
        err.multi_cell(0, 10, f"Error: {str(e)}")
        return err.output(dest='S').encode('latin-1'), "application/pdf", "Error.pdf"

# --- VERIFICACIÓN DE SUSCRIPCIÓN (CORREGIDA) ---
async def verify_active_subscription(current_user: Dict[str, Any]):
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").strip().lower()
    
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    if (email_domain in admin_domains) or (user_email in admin_emails):
        log.info(f"Acceso VIP concedido en Chat: {user_email}")
        return

    try:
        # 1. VERIFICACIÓN DIRECTA (Webhook Custom)
        # Revisamos si el documento principal del cliente tiene status 'active'
        user_doc = await db.collection("customers").document(user_id).get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            if user_data.get("status") == "active":
                return # Acceso concedido

        # 2. VERIFICACIÓN DE SUSCRIPCIONES (Stripe Extension)
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        results = [doc async for doc in query.stream()]
        
        if not results:
            # Si falla en ambos lados, denegamos
            raise HTTPException(status_code=403, detail="No tienes una suscripción activa.")
            
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        log.error(f"Error verificando suscripción DB: {e}")
        raise HTTPException(status_code=500, detail="Error interno verificando suscripción.")

def get_date_utc_minus_6() -> str:
    """Devuelve la fecha actual ajustada a la zona horaria UTC-6"""
    utc_now = datetime.now(timezone.utc)
    cst_now = utc_now - timedelta(hours=6)
    return cst_now.strftime('%Y-%m-%d')

# --- LÓGICA DE CONTROL DE LÍMITES E INCREMENTO DE USO ---
async def check_chat_limit(user_id: str, plan: str):
    """
    Verifica si el usuario superó su límite diario.
    Lanza 429 si se excede.
    """
    # Normalizar nombre del plan (ej: "Básico" -> "basico")
    plan_key = plan.lower().replace('á', 'a').strip()
    
    # Obtenemos el límite desde el diccionario que ya tiene los datos de settings
    limit = CHAT_LIMITS.get(plan_key, CHAT_LIMITS['demo']) 
    
    # -1 significa ilimitado (para admins o pruebas internas)
    if limit == -1: return

    # Usamos la fecha UTC-6
    today = get_date_utc_minus_6()
    
    # Referencia al documento de estadísticas en Firestore
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    doc = await stats_ref.get()
    current_count = 0
    
    if doc.exists:
        current_count = doc.to_dict().get('chat_count', 0)
        
    if current_count >= limit:
        # IMPORTANTE: Este error 429 activa la tarjeta roja en el Frontend
        raise HTTPException(
            status_code=429, 
            detail=f"Límite diario alcanzado para el plan {plan_key}"
        )

async def increment_chat_count(user_id: str):
    """Incrementa el contador +1 después de un éxito"""
    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    # Usamos set con merge para crear o actualizar atómicamente
    await stats_ref.set({
        'chat_count': firestore.Increment(1),
        'last_updated': firestore.SERVER_TIMESTAMP
    }, merge=True)

# --- GENERADOR STREAMING ---
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
        await asyncio.sleep(0.1) 
        
        history_from_db = await firestore_client.get_conversation_messages(user_id, convo_id)
        history_for_gemini = gemini_client.prepare_history_for_vertex(history_from_db[:-1])
        
        yield create_sse_event({"event": "status", "message": "Consultando jurisprudencia..."})
        
        search_tasks = [
            asyncio.to_thread(vertex_search_client.search, chat_request.prompt, num_results=3),
            rag_client.search_internal_documents(chat_request.prompt)
        ]
        
        combined_context = ""
        for i, task in enumerate(asyncio.as_completed(search_tasks)):
            result = await task
            combined_context += result
            yield create_sse_event({"event": "status", "message": f"Fuente {i+1} procesada..."})
        
        yield create_sse_event({"event": "status", "message": "Generando respuesta..."})
        
        final_prompt = f"Contexto geográfico: {country_code}\n{combined_context}\n\n---\n\nPregunta del usuario: {chat_request.prompt}"
        
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

        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico streaming convo {convo_id}: {e}", exc_info=True)
        error_message = json.dumps({"error": "Ocurrió un error interno al generar la respuesta."})
        yield f"data: {error_message}\n\n"

# --- ENDPOINTS ---

@app.get("/status", tags=["Status"])
def read_status():
    return {"status": "ok", "message": "PIDA Chat Backend v3.0 (PDF Fixed)"}

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
    if not title: raise HTTPException(400, "El título no puede estar vacío")
    new_convo = await firestore_client.create_new_conversation(current_user['uid'], title)
    return new_convo

@app.delete("/conversations/{convo_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat History"])
async def delete_a_conversation(convo_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user)
    await firestore_client.delete_conversation(current_user['uid'], convo_id)
    return

@app.patch("/conversations/{convo_id}/title", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat History"])
async def update_conversation_title_handler(convo_id: str, request: Request, current_user: Dict[str, Any] = Depends(get_current_user)):
    await verify_active_subscription(current_user)
    body = await request.json()
    new_title = body.get("title")
    if not new_title: raise HTTPException(400, "El título no puede estar vacío")
    await firestore_client.update_conversation_title(current_user['uid'], convo_id, new_title)
    return

# --- ENDPOINT DEL CHAT MODIFICADO CON CONTROL DE LÍMITES ---
@app.post("/chat-stream/{convo_id}", tags=["Chat"])
async def chat_stream_handler(
    convo_id: str, 
    chat_request: ChatRequest, 
    request: Request, 
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    # 1. Recuperar el Country Code (AQUÍ ESTÁ LO QUE FALTABA)
    # Viene del Frontend en el header 'X-Country-Code'
    country_code = request.headers.get('X-Country-Code', None)

    # 2. Obtener el ID y el Plan del Usuario
    user_id = current_user['uid']
    user_email = current_user.get('email', '').strip().lower()
    user_plan = 'demo' # Plan por defecto

    # --- LÓGICA DE DETECCIÓN VIP (PRIORIDAD MÁXIMA) ---
    # Si es VIP por dominio o email específico, forzamos plan 'vip' (ilimitado)
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    if (email_domain in admin_domains) or (user_email in admin_emails):
        user_plan = 'vip'
    else:
        # Si NO es VIP, buscamos su plan normal en la BD
        try:
            cust_doc = await db.collection('customers').document(user_id).get()
            if cust_doc.exists:
                data = cust_doc.to_dict()
                if data.get('status') == 'active':
                    user_plan = data.get('plan', 'basico')
                    if data.get('has_trial'):
                        user_plan = 'basico'
        except Exception as e:
            log.error(f"Error obteniendo plan usuario: {e}")

    # 3. VERIFICAR LÍMITE (Aquí se detiene y lanza error 429 si ya no tiene saldo)
    await check_chat_limit(user_id, user_plan)

    # 4. Generador Envoltorio (Para contar el uso al final)
    async def counted_stream_generator():
        # Llamamos a la IA pasando el country_code que recuperamos arriba
        async for chunk in stream_chat_response_generator(
            chat_request, 
            country_code,  # <--- Pasamos el código de país aquí
            current_user, 
            convo_id
        ):
            yield chunk
            
        # Si el stream termina exitosamente, incrementamos el contador en background
        asyncio.create_task(increment_chat_count(user_id))

    headers = { 
        "Content-Type": "text/event-stream", 
        "Cache-Control": "no-cache", 
        "Connection": "keep-alive", 
        "X-Accel-Buffering": "no" 
    }
    
    return StreamingResponse(counted_stream_generator(), headers=headers)

# --- NUEVO ENDPOINT DE DESCARGA (SOLUCIÓN PDF/DOCX) ---
@app.post("/download-chat", tags=["Chat"])
async def download_chat(
    chat_text: str = Form(...),
    title: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Genera un archivo PDF o DOCX a partir del texto del chat.
    El formato de texto esperado es:
    **Rol**: Mensaje
    ...
    """
    try:
        if file_format.lower() == "docx":
            content, mime, fname = await asyncio.to_thread(create_chat_docx_sync, chat_text, title)
        else:
            content, mime, fname = await asyncio.to_thread(create_chat_pdf_sync, chat_text, title)
            
        return Response(content=content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        log.error(f"Error descarga chat: {e}")
        raise HTTPException(500, f"Error generando archivo: {e}")

# --- NUEVO ENDPOINT DE VERIFICACIÓN VIP PARA EL FRONTEND ---
@app.post("/check-vip-access", tags=["Security"])
async def check_vip_access_handler(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").strip().lower()
    
    # 1. Chequeo Directo (Webhook Custom)
    # 1. Chequeo Directo (Webhook Custom)
    try:
        user_doc = await db.collection("customers").document(user_id).get()
        if user_doc.exists and user_doc.to_dict().get("status") == "active":
            return {"is_vip_user": True}
            
        # 2. Chequeo Suscripciones (Stripe Extension)
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        results = [doc async for doc in query.stream()]
        if results:
            return {"is_vip_user": True}
            
    except Exception as e:
        log.error(f"Error verificando suscripción DB: {e}")
        pass 
        
    # 3. Chequeo VIP/Admin (Dominios)
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    if (email_domain in admin_domains) or (user_email in admin_emails):
        return {"is_vip_user": True}
        
    return {"is_vip_user": False}

@app.post("/create-payment-intent", tags=["Billing"])
async def create_payment_intent(data: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Crea una SUSCRIPCIÓN en Stripe.
    Recibe 'name' opcional para registrar al cliente con nombre real.
    Recibe 'promotion_code' opcional para aplicar descuentos.
    """
    try:
        user_email = current_user.get("email")
        uid = current_user["uid"]
        price_id = data.get("priceId")
        plan_key = data.get("plan_key", "unknown")
        trial_days = int(data.get("trial_period_days", 0))
        customer_name = data.get("name", "") 
        
        # --- 1. LÓGICA DE CUPONES (NUEVO) ---
        user_promo_code = data.get("promotion_code", "").strip()
        promo_id = None

        if user_promo_code:
            # Buscamos en Stripe si el código de texto (ej: "PIDA20") existe y está activo
            promos = stripe.PromotionCode.list(code=user_promo_code, active=True, limit=1)
            
            if promos.data:
                # Si existe, tomamos su ID interno (ej: "promo_1Js...")
                promo_id = promos.data[0].id
            else:
                # Si no existe, detenemos todo y avisamos al frontend
                raise HTTPException(status_code=400, detail=f"El código de descuento '{user_promo_code}' no es válido o ha expirado.")

        # --- 2. BUSCAR O CREAR CLIENTE ---
        existing_customers = stripe.Customer.list(email=user_email, limit=1)
        
        if existing_customers and len(existing_customers.data) > 0:
            customer = existing_customers.data[0]
            # Opcional: Actualizar el nombre en Stripe si ya existía pero no tenía nombre
            if customer_name and not customer.name:
                stripe.Customer.modify(customer.id, name=customer_name)
        else:
            # Crear cliente nuevo
            customer_args = {"email": user_email, "metadata": {"uid": uid}}
            if customer_name:
                customer_args["name"] = customer_name
                
            customer = stripe.Customer.create(**customer_args)

        # --- 3. CREAR SUSCRIPCIÓN ---
        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{'price': price_id}],
            trial_period_days=trial_days if trial_days > 0 else None,
            promotion_code=promo_id, # <--- AQUÍ SE APLICA EL DESCUENTO
            payment_behavior='default_incomplete',
            payment_settings={'save_default_payment_method': 'on_subscription'},
            expand=['latest_invoice.payment_intent', 'pending_setup_intent'], 
            metadata={
                "uid": uid,
                "email": user_email,
                "plan_key": plan_key,
                "trial_days": str(trial_days)
            }
        )

        # --- 4. DETERMINAR QUÉ SECRETO DEVOLVER ---
        if trial_days > 0 and subscription.pending_setup_intent:
            client_secret = subscription.pending_setup_intent.client_secret
        else:
            client_secret = subscription.latest_invoice.payment_intent.client_secret

        return {
            "subscriptionId": subscription.id,
            "clientSecret": client_secret
        }

    except HTTPException as http_e:
        # Re-lanzar errores HTTP específicos (como el del cupón inválido)
        raise http_e
    except Exception as e:
        log.error(f"Error creando Suscripción: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- NOTA: Esta línea debe estar pegada al borde izquierdo (sin espacios) ---
@app.post("/stripe-webhook", tags=["Billing"])
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    
    try:
        # Usa tu variable forzada o settings según corresponda
        webhook_secret = settings.STRIPE_WEBHOOK_SECRET # O FORCED_WEBHOOK_SECRET si sigues hardcodeado
        
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
        
        data_object = event['data']['object']
        uid = None
        plan_key = None
        
        # LÓGICA DE DEDUCCIÓN DE PLAN
        def get_plan_from_obj(obj):
            # 1. Intentar por Precio (Lo más seguro para cambios en Portal)
            if 'items' in obj and 'data' in obj['items'] and len(obj['items']['data']) > 0:
                current_price_id = obj['items']['data'][0]['price']['id']
                if current_price_id in STRIPE_PRICE_MAP:
                    return STRIPE_PRICE_MAP[current_price_id]
            
            # 2. Intentar por Metadata (Respaldo)
            return obj.get('metadata', {}).get('plan_key')

        # --- CASO 1: SUSCRIPCIONES (Creada o Actualizada) ---
        if event['type'] in ['customer.subscription.created', 'customer.subscription.updated']:
            uid = data_object.get('metadata', {}).get('uid')
            
            # ¡AQUÍ ESTÁ LA CORRECCIÓN!
            plan_key = get_plan_from_obj(data_object)
            
            status = data_object.get('status')
            
            # Aceptamos 'active' y 'trialing'
            if status in ['active', 'trialing']:
                if uid and plan_key:
                    await db.collection("customers").document(uid).set({
                        "status": "active",
                        "stripe_subscription_id": data_object.get('id'),
                        "plan": plan_key, # Ahora sí se actualizará a "premium"
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }, merge=True)
                    log.info(f"Suscripción actualizada para {uid}: Plan {plan_key} ({status})")

        # --- CASO 2: FACTURA PAGADA ---
        elif event['type'] == 'invoice.payment_succeeded':
            subscription_id = data_object.get('subscription')
            if subscription_id:
                # Recuperamos la suscripción para ver el precio actual actualizado
                sub = stripe.Subscription.retrieve(subscription_id)
                uid = sub.get('metadata', {}).get('uid')
                
                # ¡AQUÍ TAMBIÉN! Usamos el precio actual de la suscripción, no la metadata vieja
                plan_key = get_plan_from_obj(sub)
                
                if uid and plan_key:
                    await db.collection("customers").document(uid).set({
                        "status": "active",
                        "stripe_subscription_id": subscription_id,
                        "plan": plan_key,
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }, merge=True)

    except Exception as e:
        log.error(f"Error procesando Webhook: {e}")
        return Response(content=str(e), status_code=500)

    return {"status": "success"}

@app.post("/create-portal-session", tags=["Billing"])
async def create_portal_session(request: Request, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Genera una URL para el Portal de Facturación.
    Ahora acepta 'return_url' dinámicamente para soportar entornos de prueba.
    """
    try:
        # 1. LEER URL DINÁMICA DEL FRONTEND
        try:
            body = await request.json()
            return_url = body.get("return_url")
        except Exception:
            return_url = None
        
        # Si no nos mandan nada (por error), usamos producción como respaldo
        if not return_url:
            return_url = "https://pida-ai.com/"

        user_email = current_user.get("email")
        
        # 2. Buscar cliente en Stripe
        customers = stripe.Customer.list(email=user_email, limit=1)
        
        if not customers.data:
            raise HTTPException(status_code=404, detail="No se encontró un cliente asociado a este correo en Stripe.")

        stripe_customer_id = customers.data[0].id

        # 3. Crear sesión con la URL de retorno CORRECTA
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url # <--- Aquí sucede la magia
        )

        return {"url": session.url}

    except Exception as e:
        log.error(f"Error generando sesión del portal: {e}")
        raise HTTPException(status_code=500, detail=str(e))