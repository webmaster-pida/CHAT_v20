# src/config.py

import logging
import google.cloud.logging
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Configuración de Logging para Google Cloud ---
# Esto conecta los logs de la aplicación con la consola de GCP
client = google.cloud.logging.Client()
client.setup_logging()
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
    PSE_API_KEY: str
    PSE_ID: str
    
    # Nueva variable para la URL del RAG (Agrégala en Cloud Run)
    RAG_API_URL: str

    # --- Variables del Modelo Generativo (Opcionales con default) ---
    MAX_OUTPUT_TOKENS: int = 16384
    TEMPERATURE: float = 0.7
    TOP_P: float = 0.95

    # --- VARIABLES DE CONTROL DE ACCESO (Formato JSON String) ---
    # Estos valores por defecto se usarán si NO defines las variables en Cloud Run.
    # Recomendación: Define los valores reales en la consola de Cloud Run para mayor seguridad.

    # 1. Orígenes permitidos (CORS) - Quién puede conectar con el backend
    ALLOWED_ORIGINS: str = '["https://pida.iiresodh.org", "https://pida-ai.com", "http://localhost", "http://localhost:8080"]'

    # 2. Dominios corporativos permitidos (ej: todos los @iiresodh.org entran)
    ADMIN_DOMAINS: str = '["iiresodh.org", "urquilla.com"]'

    # 3. Usuarios específicos permitidos (ej: gmail personales o externos)
    ADMIN_EMAILS: str = '[]'

# Instanciamos la configuración para importarla en otros módulos
settings = Settings()
