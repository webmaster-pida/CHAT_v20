# src/config.py

import logging
import json
from typing import List, Union, Optional
import google.cloud.logging
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Configuración de Logging para Google Cloud ---
try:
    client = google.cloud.logging.Client()
    client.setup_logging()
except Exception:
    pass 

log = logging.getLogger("pida-backend")
log.setLevel(logging.INFO)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    # --- NUEVO: API DE PERPLEXITY ---
    PERPLEXITY_API_KEY: str = ""
    PERPLEXITY_MODEL: str = "sonar-pro"

    # --- Variables de Google Cloud y API ---
    GOOGLE_CLOUD_PROJECT: str = "pida-ai-v20"
    GOOGLE_CLOUD_LOCATION: str = "us-central1"
    GEMINI_MODEL: str = "gemini-2.5-pro"
    
    # URL del RAG
    RAG_API_URL: str = "https://rag-v20-genai-465781488910.us-central1.run.app/query"

    # --- Variables del Modelo Generativo ---
    MAX_OUTPUT_TOKENS: int = 16384
    TEMPERATURE: float = 0.5
    TOP_P: float = 0.8

    # --- VARIABLES DE STRIPE ---
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # --- VARIABLES DE LÍMITES DE CHAT (NUEVO) ---
    # Estos valores actúan como "default". Si en Cloud Run defines la variable de entorno,
    # Pydantic tomará el valor de Cloud Run automáticamente.
    LIMIT_FREEMIUM_CHAT_MONTHLY: int = 5    # Límite para usuarios sin tarjeta
    LIMIT_BASICO_CHAT_MONTHLY: int = 150    # Antes DAILY: 5
    LIMIT_AVANZADO_CHAT_MONTHLY: int = 600  # Antes DAILY: 20
    LIMIT_PREMIUM_CHAT_MONTHLY: int = 3000  # Antes DAILY: 100
    MAX_EXPORT_LENGTH: int = 150000

    # --- CONTROL DE ACCESO ---
    ALLOWED_ORIGINS: Union[str, List[str]] = '["https://pida.iiresodh.org", "https://pida-ai.com", "https://pida-ai-v20.web.app", "http://localhost", "http://localhost:8080"]'
    ADMIN_DOMAINS: Union[str, List[str]] = '["iiresodh.org", "urquilla.com"]'
    ADMIN_EMAILS: Union[str, List[str]] = '[]'

    # --- VALIDADORES ---
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

settings = Settings()
