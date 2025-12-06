import json
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from src.config import settings, log

# --- Inicialización de Firebase Admin ---
# Esto es seguro en Cloud Run, ya que usa las credenciales del entorno.
try:
    cred = credentials.ApplicationDefault()
    # Inicializamos sin argumentos extra, asumiendo que el entorno de Cloud Run
    # provee la identidad correcta. Si hubiera problemas de proyecto cruzado,
    # se podría agregar {'projectId': 'TU_PROJECT_ID'} aquí.
    firebase_admin.initialize_app(cred)
except ValueError:
    # Esto evita que la app crashee si se inicializa múltiples veces (común en desarrollo)
    pass

# Esta instancia no se usa para validar, solo para que FastAPI muestre el candado en la documentación.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- EL PORTERO ---
async def get_current_user(request: Request):
    """
    Dependencia para verificar el token de Firebase ID enviado en la cabecera Authorization.
    Aplica reglas de seguridad basadas en Dominios y Correos permitidos configurados en Cloud Run.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar la firma del token con Firebase (garantiza que es un usuario real de Google)
        decoded_token = auth.verify_id_token(token)
        
        # 2. Obtener datos del usuario
        email = decoded_token.get("email", "").lower()
        domain = email.split("@")[1] if "@" in email else ""
        
        # 3. Cargar reglas de acceso desde la configuración (LÓGICA ROBUSTA)
        try:
            # --- MANEJO DE DOMINIOS ---
            raw_domains = settings.ADMIN_DOMAINS
            # Si Pydantic ya lo convirtió a lista, la usamos directo. Si es string, parseamos JSON.
            if isinstance(raw_domains, list):
                allowed_domains = raw_domains
            elif isinstance(raw_domains, str) and raw_domains.strip():
                allowed_domains = json.loads(raw_domains)
            else:
                allowed_domains = []
            
            # Limpieza: Aseguramos minúsculas y sin espacios
            allowed_domains = [str(d).strip().lower() for d in allowed_domains]

            # --- MANEJO DE EMAILS ---
            raw_emails = settings.ADMIN_EMAILS
            if isinstance(raw_emails, list):
                allowed_emails = raw_emails
            elif isinstance(raw_emails, str) and raw_emails.strip():
                allowed_emails = json.loads(raw_emails)
            else:
                allowed_emails = []

            # Limpieza: Aseguramos minúsculas y sin espacios
            allowed_emails = [str(e).strip().lower() for e in allowed_emails]

        except Exception as e:
            # Logueamos el error crítico pero no detenemos la app (se asumen listas vacías)
            log.error(f"CRITICAL: Error cargando listas de acceso. Error: {e}")
            allowed_domains = []
            allowed_emails = []

        # 4. APLICAR LÓGICA DE SEGURIDAD
        # Solo aplicamos el filtro si existe AL MENOS UNA regla configurada.
        has_restrictions = bool(allowed_domains or allowed_emails)
        
        if has_restrictions:
            # El usuario pasa si su dominio está en la lista O si su email específico está en la lista
            is_domain_authorized = domain in allowed_domains
            is_email_authorized = email in allowed_emails
            
            if not (is_domain_authorized or is_email_authorized):
                log.warning(f"⛔ ACCESO DENEGADO: {email}. Dominio '{domain}' no autorizado.")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes autorización para acceder a esta plataforma. Contacta al administrador."
                )

        # Si pasa las validaciones, devolvemos el token decodificado
        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token de sesión ha expirado. Por favor recarga la página.",
        )
    except auth.InvalidIdTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"El token es inválido: {e}",
        )
    except HTTPException as he:
        # Re-lanzamos las excepciones HTTP propias (como el 403 Forbidden creado arriba)
        raise he
    except Exception as e:
        log.error(f"Error inesperado en autenticación: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno durante la verificación de seguridad.",
        )
