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
    model_config = SettingsConfigDict(extra='ignore')

    # --- Variables de Google Cloud y API ---
    GOOGLE_CLOUD_PROJECT: str = "pida-ai-v20"
    GOOGLE_CLOUD_LOCATION: str = "us-central1"
    GEMINI_MODEL: str = "gemini-2.5-pro"
    
    # Vertex Search (Con valores por defecto para evitar crashes)
    VERTEX_SEARCH_PROJECT_ID: str = "pida-ai-v20"
    VERTEX_SEARCH_LOCATION: str = "global"
    VERTEX_SEARCH_DATA_STORE_ID: str = "almacen-web-pida_1765039607916"

    # PSE (Búsqueda antigua) - Las mantenemos opcionales o con string vacío para que no rompan el inicio
    # Si Cloud Run las tiene configuradas, las usará. Si no, usará "".
    PSE_API_KEY: str = ""
    PSE_ID: str = ""
    
    # URL del RAG (CRÍTICO: Agregamos el default aquí para corregir tu error)
    RAG_API_URL: str = "https://pida-rag-api-640849120264.us-central1.run.app/query"

    # --- Variables del Modelo Generativo ---
    MAX_OUTPUT_TOKENS: int = 16384
    TEMPERATURE: float = 0.7
    TOP_P: float = 0.95

    # --- VARIABLES DE STRIPE ---
    STRIPE_SECRET_KEY: str = "sk_test_51RMB12GaDEQrzamxdZLI00ipZKlSazwI0ZX22yztJJR0eTh9R3QejzbZbje10YfeZRjzKoMl4l1gQqZqaGp6IY2V00lK4zOIe4"
    STRIPE_WEBHOOK_SECRET: str = "whsec_KNABkl3vVmx4OL1qQ5pIghq8rmmsFQ0a"
    
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
