# src/core/security.py

import json
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from src.config import settings, log

# --- Inicialización de Firebase Admin ---
try:
    # Se intenta inicializar con las credenciales por defecto de Google Cloud
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
except ValueError:
    # Si ya está inicializado (por ejemplo, en hot-reload), ignoramos el error
    pass

# Esquema para documentación de Swagger/OpenAPI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- EL PORTERO (SOLO AUTENTICACIÓN) ---
async def get_current_user(request: Request):
    """
    Dependencia para verificar el token de Firebase ID.
    Ahora solo verifica que el token sea válido. La lógica de acceso
    (si es VIP o tiene Stripe) se maneja en 'verify_active_subscription'.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar la firma y validez del token con Firebase
        decoded_token = auth.verify_id_token(token)
        
        # 2. Retornamos el token decodificado
        # Esto permite que usuarios de cualquier dominio (como gmail.com) pasen
        # este filtro y lleguen a la validación de suscripción en main.py.
        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token de sesión ha expirado.",
        )
    except auth.InvalidIdTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"El token es inválido o está mal formado: {e}",
        )
    except Exception as e:
        log.error(f"Error inesperado en el proceso de autenticación: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servicio de seguridad.",
        )
