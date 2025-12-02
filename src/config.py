# src/config.py
import logging
import google.cloud.logging
from pydantic_settings import BaseSettings, SettingsConfigDict

client = google.cloud.logging.Client()
client.setup_logging()
log = logging.getLogger("pida-backend")
log.setLevel(logging.INFO)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    # --- Variables de Google Cloud y API ---
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_LOCATION: str
    GEMINI_MODEL: str
    PSE_API_KEY: str
    PSE_ID: str
    RAG_API_URL: str

    # --- Variables Modelo ---
    MAX_OUTPUT_TOKENS: int = 16384
    TEMPERATURE: float = 0.7
    TOP_P: float = 0.95

    # --- NUEVAS VARIABLES DE CONTROL DE ACCESO (Formato JSON String) ---
    # Valores por defecto actuales para evitar caídas si no se configuran las ENV vars
    ALLOWED_ORIGINS: str = '["https://pida.iiresodh.org", "https://pida-ai.com", "http://localhost", "http://localhost:8080"]'
    ADMIN_DOMAINS: str = '["iiresodh.org", "urquilla.com"]'
    ADMIN_EMAILS: str = '[]'

settings = Settings()
