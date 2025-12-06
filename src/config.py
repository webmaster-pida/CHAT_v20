# src/config.py

import logging
import json
from typing import List, Union
import google.cloud.logging
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Configuración de Logging para Google Cloud ---
# Esto conecta los logs de la aplicación con la consola de GCP
try:
    client = google.cloud.logging.Client()
    client.setup_logging()
except Exception:
    pass # Fallback para desarrollo local sin credenciales de GCP

log = logging.getLogger("pida-backend")
log.setLevel(logging.INFO)

class Settings(BaseSettings):
    # Configuración para que lea automáticamente del archivo .env (local) 
    # o de las Variables de Entorno (Cloud Run)
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    # --- Variables de Google Cloud y API (OBLIGATORIAS) ---
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_LOCATION: str
    GEMINI_MODEL: str
    
    # Vertex Search (NUEVAS VARIABLES - Agrégalas a tu .env o Cloud Run)
    VERTEX_SEARCH_PROJECT_ID: str = "pida-ai-v20" # Valor por defecto o cambiar por variable de entorno
    VERTEX_SEARCH_LOCATION: str = "global"
    VERTEX_SEARCH_DATA_STORE_ID: str = "almacen-web-pida_1765039607916"

    PSE_API_KEY: str
    PSE_ID: str
    
    # URL del RAG
    RAG_API_URL: str

    # --- Variables del Modelo Generativo (Opcionales con default) ---
    MAX_OUTPUT_TOKENS: int = 16384
    TEMPERATURE: float = 0.7
    TOP_P: float = 0.95

    # --- VARIABLES DE CONTROL DE ACCESO ---
    # Se definen como Union[str, List[str]] para aceptar JSON string o lista directa
    ALLOWED_ORIGINS: Union[str, List[str]] = '["https://pida.iiresodh.org", "https://pida-ai.com", "http://localhost", "http://localhost:8080"]'
    ADMIN_DOMAINS: Union[str, List[str]] = '["iiresodh.org", "urquilla.com"]'
    ADMIN_EMAILS: Union[str, List[str]] = '[]'

    # --- VALIDADORES AUTOMÁTICOS (Lógica DRY) ---
    # Estos métodos convierten automáticamente los strings JSON a listas limpias de Python
    
    @field_validator('ALLOWED_ORIGINS', 'ADMIN_DOMAINS', 'ADMIN_EMAILS', mode='before')
    @classmethod
    def parse_json_list(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, list):
            return [str(item).strip().lower() for item in v]
        if isinstance(v, str) and v.strip():
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item).strip().lower() for item in parsed]
            except json.JSONDecodeError:
                log.error(f"Error decodificando configuración JSON: {v}")
                return []
        return []

# Instanciamos la configuración para importarla en otros módulos
settings = Settings()
