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

# MAPA DE TRADUCCIÓN: ID de Stripe -> Nombre del Plan interno para que no se equivoque
STRIPE_PRICE_MAP = {
    # BÁSICO
    "price_1SqFQiGgaloBN5L8U60ywohe": "basico", 
    "price_1SqFSFGgaloBN5L8kxegWZqC": "basico", 
    "price_1SqFSFGgaloBN5L8BMBeRPqb": "basico", 
    "price_1SqFSyGgaloBN5L8rrwrtUau": "basico", 
    
    # AVANZADO
    "price_1SqFUvGgaloBN5L8xOBssn6E": "avanzado",
    "price_1SqFWJGgaloBN5L8VKhkzLRH": "avanzado",
    "price_1SqFWJGgaloBN5L8roECNay2": "avanzado",
    "price_1SqFWJGgaloBN5L8hKpEvd1v": "avanzado",

    # PREMIUM
    "price_1SqFXIGgaloBN5L8vaGyleDT": "premium",
    "price_1SqFadGgaloBN5L86iwNYm1c": "premium",
    "price_1SqFadGgaloBN5L8AwTUeTSd": "premium",
    "price_1SqFadGgaloBN5L8QFHXe1i9": "premium",
}

# --- LÍMITES DE CHAT (POR PREGUNTA) ---
CHAT_LIMITS = {
    "basico": settings.LIMIT_BASICO_CHAT_DAILY,      # 5
    "avanzado": settings.LIMIT_AVANZADO_CHAT_DAILY,  # 20
    "premium": settings.LIMIT_PREMIUM_CHAT_DAILY,    # 100
    "vip": -1                                        # Ilimitado
}

# Inicializar Stripe
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
    allow_origin_regex=r"https://pida-ai-v20--.*\.web\.app$|https://.*\.app\.github\.dev$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CLIENTE FIRESTORE ASÍNCRONO ---
db = firestore.AsyncClient()

# --- UTILIDADES DE DOCUMENTOS ---
def generate_filename(title: str, extension: str) -> str:
    safe_title = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ]', '', title[:40])
    safe_title = safe_title.strip().replace(' ', '_')
    if not safe_title: safe_title = "Chat_PIDA"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return f"{safe_title}_{timestamp}.{extension}"

def sanitize_text_for_pdf(text: str) -> str:
    if not text: return ""
    replacements = { "•": "-", "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...", "\u2013": "-", "\u2014": "-", "\u2022": "-", "\uF0B7": "-" }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    return text.encode('latin1', 'replace').decode('latin-1')

def write_markdown_to_pdf(pdf, text):
    pdf.set_font("Arial", "", 11)
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            pdf.ln(5)
            continue
        if line.startswith('**') and ':**' in line:
            parts = line.split(':**', 1)
            role = parts[0].replace('**', '')
            content = parts[1].strip()
            pdf.set_font("Arial", "B", 11)
            pdf.set_text_color(29, 53, 87) 
            pdf.write(6, f"{role}: ")
            pdf.set_font("Arial", "", 11)
            pdf.set_text_color(0, 0, 0)
            sub_parts = re.split(r'(\*\*.*?\*\*)', content)
            for sp in sub_parts:
                if sp.startswith('**') and sp.endswith('**'):
                    pdf.set_font("Arial", "B", 11)
                    pdf.write(6, sp.strip('*'))
                    pdf.set_font("Arial", "", 11)
                else:
                    pdf.write(6, sp)
            pdf.ln(6)
        elif line.startswith('## '):
            pdf.ln(3)
            pdf.set_font("Arial", "B", 13)
            pdf.set_text_color(29, 53, 87)
            pdf.multi_cell(0, 8, line.replace('## ', ''))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Arial", "", 11)
        elif line.startswith('* ') or line.startswith('- '):
            pdf.set_x(15)
            pdf.write(6, "- " + line[2:])
            pdf.ln(6)
        else:
            pdf.multi_cell(0, 6, line)

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
    pdf.set_font("Arial", "B", 12)
    pdf.multi_cell(0, 6, f"Tema: {safe_title}")
    pdf.ln(5)
    if not safe_text.strip():
        pdf.multi_cell(0, 6, "[Chat vacío]")
    else:
        write_markdown_to_pdf(pdf, safe_text)
    try:
        pdf_string = pdf.output(dest='S')
        if isinstance(pdf_string, str): pdf_bytes = pdf_string.encode('latin-1', 'replace')
        else: pdf_bytes = pdf_string
        stream = io.BytesIO(pdf_bytes)
        fname = generate_filename(title, "pdf")
        return stream.read(), "application/pdf", fname
    except Exception as e:
        err = FPDF()
        err.add_page()
        err.multi_cell(0, 10, f"Error: {str(e)}")
        return err.output(dest='S').encode('latin-1'), "application/pdf", "Error.pdf"

# --- VERIFICACIÓN DE SUSCRIPCIÓN ---
async def verify_active_subscription(current_user: Dict[str, Any]):
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").strip().lower()
    
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    if (email_domain in admin_domains) or (user_email in admin_emails):
        return

    try:
        user_doc = await db.collection("customers").document(user_id).get()
        if user_doc.exists and user_doc.to_dict().get("status") == "active":
            return # Único punto de entrada permitido
        
        raise HTTPException(status_code=403, detail="Suscripción inactiva o requiere tarjeta válida.")
    except HTTPException as e: raise e
    except Exception as e:
        log.error(f"Error Verificación: {e}")
        raise HTTPException(status_code=500, detail="Error de servidor.")
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        log.error(f"Error verificando suscripción DB: {e}")
        raise HTTPException(status_code=500, detail="Error interno verificando suscripción.")

def get_date_utc_minus_6() -> str:
    utc_now = datetime.now(timezone.utc)
    cst_now = utc_now - timedelta(hours=6)
    return cst_now.strftime('%Y-%m-%d')

# --- LÓGICA DE CONTROL DE LÍMITES POR PREGUNTA ---
async def check_chat_limit(user_id: str, plan: str):
    """
    Verifica si el usuario superó su límite diario de PREGUNTAS.
    """
    plan_key = plan.lower().replace('á', 'a').strip()
    limit = CHAT_LIMITS.get(plan_key, 0)
    
    if limit == -1: return # VIP ilimitado

    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    doc = await stats_ref.get()
    current_count = 0
    
    if doc.exists:
        current_count = doc.to_dict().get('chat_count', 0)
        
    if current_count >= limit:
        # Esto activa el modal en el frontend
        raise HTTPException(
            status_code=429, 
            detail=f"Límite diario alcanzado para el plan {plan_key}"
        )

async def increment_chat_count(user_id: str):
    """Incrementa el contador +1 (se llama solo tras respuesta exitosa)"""
    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
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
        # Guardar mensaje usuario
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

        # Si se generó respuesta, guardar mensaje modelo
        if full_response_text:
            model_message = ChatMessage(role="model", content=full_response_text)
            await firestore_client.add_message_to_conversation(user_id, convo_id, model_message)

        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico streaming convo {convo_id}: {e}", exc_info=True)
        # Yield error para que el cliente (y el contador) lo sepan
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
    """Crea una conversación vacía. NO incrementa contador aquí."""
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

# --- ENDPOINT DEL CHAT MODIFICADO CON CONTROL DE LÍMITES POR PREGUNTA ---
@app.post("/chat-stream/{convo_id}", tags=["Chat"])
async def chat_stream_handler(
    convo_id: str, 
    chat_request: ChatRequest, 
    request: Request, 
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    # 1. Recuperar el Country Code
    country_code = request.headers.get('X-Country-Code', None)

    # 2. Obtener el ID y el Plan del Usuario
    user_id = current_user['uid']
    user_email = current_user.get('email', '').strip().lower()
    user_plan = 'none' 

    # --- LÓGICA DE DETECCIÓN VIP ---
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    if (email_domain in admin_domains) or (user_email in admin_emails):
        user_plan = 'vip'
    else:
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

    # 4. Generador Envoltorio (Para contar el uso SOLO si hay respuesta exitosa)
    async def counted_stream_generator():
        has_error = False
        full_content_received = False
        
        # Llamamos a la IA pasando el country_code
        async for chunk in stream_chat_response_generator(
            chat_request, 
            country_code, 
            current_user, 
            convo_id
        ):
            # Detectar si el chunk reporta un error
            if '"error":' in chunk:
                has_error = True
            
            # Detectar si estamos recibiendo contenido real (no solo eventos de estado)
            if '"text":' in chunk and not has_error:
                full_content_received = True
                
            yield chunk
            
        # SOLO INCREMENTAMOS SI:
        # 1. No hubo errores
        # 2. Se recibió contenido real
        if not has_error and full_content_received:
            asyncio.create_task(increment_chat_count(user_id))

    headers = { 
        "Content-Type": "text/event-stream", 
        "Cache-Control": "no-cache", 
        "Connection": "keep-alive", 
        "X-Accel-Buffering": "no" 
    }
    
    return StreamingResponse(counted_stream_generator(), headers=headers)

@app.post("/download-chat", tags=["Chat"])
async def download_chat(
    chat_text: str = Form(...),
    title: str = Form(...),
    file_format: str = Form("docx"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    try:
        if file_format.lower() == "docx":
            content, mime, fname = await asyncio.to_thread(create_chat_docx_sync, chat_text, title)
        else:
            content, mime, fname = await asyncio.to_thread(create_chat_pdf_sync, chat_text, title)
        return Response(content=content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        log.error(f"Error descarga chat: {e}")
        raise HTTPException(500, f"Error generando archivo: {e}")

@app.post("/check-vip-access", tags=["Security"])
async def check_vip_access_handler(current_user: Dict[str, Any] = Depends(get_current_user)):
    user_email = current_user.get("email", "").strip().lower()
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""
    if (email_domain in admin_domains) or (user_email in admin_emails):
        return {"is_vip_user": True}
    return {"is_vip_user": False}

@app.post("/validate-promo-code", tags=["Billing"])
async def validate_promo_code(request: Request):
    """
    Valida cupón usando Metadatos y el mapa de precios plano de Python.
    """
    try:
        data = await request.json()
        promo_code = data.get("code", "").strip()
        price_id = data.get("priceId")

        print(f"--- VALIDANDO CUPÓN (METADATA FIXED) ---")
        print(f"Código: {promo_code} | Precio ID: {price_id}")

        if not promo_code or not price_id:
            raise HTTPException(status_code=400, detail="Faltan datos requeridos.")

        # 1. Identificar el Plan Interno (CORREGIDO)
        # STRIPE_PRICE_MAP en Python es { "price_id": "nombre_plan" }
        # No necesitamos bucles, solo un acceso directo.
        current_plan_name = STRIPE_PRICE_MAP.get(price_id)
        
        print(f"Plan Identificado internamente: {current_plan_name}")

        if not current_plan_name:
             # Si el ID no está en nuestro mapa, es un error de configuración o el ID cambió
             raise HTTPException(status_code=400, detail="El plan seleccionado no es válido en el sistema.")

        # 2. Buscar el código de promoción en Stripe
        promos = stripe.PromotionCode.list(code=promo_code, active=True, limit=1)
        
        if not promos.data:
            raise HTTPException(status_code=404, detail="El código promocional no es válido o ha expirado.")

        promo_obj = promos.data[0]
        coupon_id = promo_obj.coupon.id
        
        # 3. Recuperar cupón explícitamente
        try:
            coupon = stripe.Coupon.retrieve(coupon_id)
        except Exception as e:
            print(f"Error recuperando cupón: {e}")
            raise HTTPException(status_code=500, detail="Error de conexión con Stripe.")

        # 4. VALIDACIÓN DE RESTRICCIONES (PRIORIDAD: METADATA)
        
        # A) Chequeo por Metadata 'allowed_plans'
        allowed_plans_meta = coupon.metadata.get("allowed_plans")
        
        if allowed_plans_meta:
            # Convertimos "premium, avanzado" en lista ['premium', 'avanzado']
            allowed_list = [p.strip().lower() for p in allowed_plans_meta.split(",")]
            print(f"Planes permitidos por Metadata: {allowed_list}")
            
            if current_plan_name not in allowed_list:
                print(f"❌ BLOQUEO POR METADATA: {current_plan_name} no está en {allowed_list}")
                raise HTTPException(
                    status_code=400, 
                    detail=f"Este cupón solo es válido para el plan {allowed_plans_meta.upper()}."
                )
            else:
                print("✅ Validación Metadata Exitosa.")
        
        # B) Chequeo nativo (Fallback)
        elif coupon.get("applies_to"):
            try:
                price_obj = stripe.Price.retrieve(price_id)
                current_product_id = price_obj.product
                allowed_products = coupon.applies_to.get("products", [])
                
                if allowed_products and current_product_id not in allowed_products:
                    print(f"❌ BLOQUEO NATIVO: Producto {current_product_id} no permitido.")
                    raise HTTPException(status_code=400, detail="Código no válido para este plan.")
            except Exception:
                pass 

        else:
            print("⚠️ Advertencia: Cupón sin restricciones explícitas. Se aplicará.")

        # 5. Obtener precio para cálculo
        price_obj = stripe.Price.retrieve(price_id)
        original_amount = price_obj.unit_amount 
        currency = price_obj.currency.upper()

        # 6. Cálculo matemático
        final_amount = original_amount
        discount_desc = ""

        if coupon.percent_off:
            discount_amount = int(round(original_amount * (coupon.percent_off / 100)))
            final_amount = original_amount - discount_amount
            discount_desc = f"-{coupon.percent_off}%"
        
        elif coupon.amount_off:
            if coupon.currency.upper() != currency:
                raise HTTPException(status_code=400, detail=f"Moneda incorrecta.")
            
            final_amount = original_amount - coupon.amount_off
            discount_desc = f"-${coupon.amount_off / 100:.2f} {currency}"

        if final_amount < 0: final_amount = 0

        return {
            "valid": True,
            "code": promo_obj.code,
            "original_amount": original_amount,
            "final_amount": final_amount,
            "currency": currency,
            "description": discount_desc,
            "coupon_name": coupon.name or promo_obj.code,
            "promo_id": promo_obj.id
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        log.error(f"Error validando promo: {e}")
        print(f"EXCEPTION FINAL: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

@app.post("/create-payment-intent", tags=["Billing"])
async def create_payment_intent(data: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)):
    try:
        user_email = current_user.get("email")
        uid = current_user["uid"]
        price_id = data.get("priceId")
        plan_key = data.get("plan_key", "unknown")
        trial_days = int(data.get("trial_period_days", 0))
        customer_name = data.get("name", "") 
        user_promo_code = data.get("promotion_code", "").strip()

        customer = None
        # --- BÚSQUEDA DE CLIENTE (UN SOLO BLOQUE) ---
        search_query = f"metadata['firebaseUID']:'{uid}' OR metadata['uid']:'{uid}'"
        search_result = stripe.Customer.search(query=search_query, limit=1)
        if search_result.data:
            customer = search_result.data[0]
        else:
            existing_customers = stripe.Customer.list(email=user_email, limit=1)
            if existing_customers.data:
                customer = existing_customers.data[0]

        if not customer:
            customer = stripe.Customer.create(
                email=user_email, 
                name=customer_name, 
                metadata={"uid": uid, "firebaseUID": uid}
            )
        else:
            if customer_name and customer.name != customer_name:
                stripe.Customer.modify(customer.id, name=customer_name)

        promo_id = None
        if user_promo_code:
            promos = stripe.PromotionCode.list(code=user_promo_code, active=True, limit=1)
            if promos.data: promo_id = promos.data[0].id
            else: raise HTTPException(status_code=400, detail=f"Código inválido.")

        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{'price': price_id}],
            trial_period_days=trial_days if trial_days > 0 else None,
            promotion_code=promo_id,
            payment_behavior='default_incomplete',
            payment_settings={'save_default_payment_method': 'on_subscription'},
            expand=['latest_invoice.payment_intent', 'pending_setup_intent'], 
            metadata={"uid": uid, "plan_key": plan_key}
        )

        if trial_days > 0 and subscription.pending_setup_intent:
            client_secret = subscription.pending_setup_intent.client_secret
        else:
            client_secret = subscription.latest_invoice.payment_intent.client_secret
            
        return {"subscriptionId": subscription.id, "clientSecret": client_secret}
    except Exception as e:
        log.error(f"Error Suscripción: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stripe-webhook", tags=["Billing"])
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    
    try:
        # 1. Validar firma
        webhook_secret = settings.STRIPE_WEBHOOK_SECRET 
        if not webhook_secret:
            log.error("⚠️ STRIPE_WEBHOOK_SECRET no está configurado en las variables de entorno.")
            return Response(content="Webhook secret missing", status_code=500)

        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        data_object = event['data']['object']
        
        log.info(f"📩 Webhook recibido: {event['type']}")

        def resolve_plan(sub_obj):
            items = sub_obj.get('items', {}).get('data', [])
            p_id = items[0]['price']['id'] if items else None
            return STRIPE_PRICE_MAP.get(p_id, "basico")

        if event['type'] in ['customer.subscription.created', 'customer.subscription.updated']:
            subscription = data_object
            uid = subscription.get('metadata', {}).get('uid')
            stripe_status = subscription.get('status')
            
            # CRÍTICO: Una suscripción solo es válida si Stripe confirma el pago/trial 
            # Y existe un método de pago vinculado (evita el bypass de tarjetas falsas)
            has_pm = subscription.get('default_payment_method') is not None or \
                     subscription.get('default_source') is not None
            
            if uid:
                is_active = (stripe_status in ['active', 'trialing']) and has_pm
                
                await db.collection("customers").document(uid).set({
                    "status": "active" if is_active else "inactive",
                    "plan": resolve_plan(subscription) if is_active else "none",
                    "stripe_status": stripe_status,
                    "updated_at": firestore.SERVER_TIMESTAMP
                }, merge=True)
                log.info(f"🛡️ Webhook: {uid} set to {'active' if is_active else 'inactive'} ({stripe_status})")

        elif event['type'] in ['customer.subscription.deleted', 'invoice.payment_failed']:
            uid = data_object.get('metadata', {}).get('uid')
            if not uid and data_object.get('subscription'):
                sub = stripe.Subscription.retrieve(data_object['subscription'])
                uid = sub.get('metadata', {}).get('uid')
            
            if uid:
                await db.collection("customers").document(uid).set({
                    "status": "inactive", "plan": "none", "updated_at": firestore.SERVER_TIMESTAMP
                }, merge=True)

        return {"status": "success"}

    except stripe.error.SignatureVerificationError as e:
        log.error(f"❌ Firma de Webhook inválida: {e}")
        return Response(content="Invalid signature", status_code=400)
    except Exception as e:
        # ESTO ES LO QUE NOS DIRÁ EL ERROR REAL EN LOS LOGS
        log.error(f"💥 Error crítico en Webhook: {str(e)}", exc_info=True)
        return Response(content=str(e), status_code=500)

@app.post("/create-portal-session", tags=["Billing"])
async def create_portal_session(request: Request, current_user: Dict[str, Any] = Depends(get_current_user)):
    try:
        try:
            body = await request.json()
            return_url = body.get("return_url")
        except Exception: return_url = None
        if not return_url: return_url = "https://pida-ai.com/"
        user_email = current_user.get("email")
        customers = stripe.Customer.list(email=user_email, limit=1)
        if not customers.data: raise HTTPException(status_code=404, detail="No se encontró un cliente asociado a este correo en Stripe.")
        stripe_customer_id = customers.data[0].id
        session = stripe.billing_portal.Session.create(customer=stripe_customer_id, return_url=return_url)
        return {"url": session.url}
    except Exception as e:
        log.error(f"Error generando sesión del portal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

