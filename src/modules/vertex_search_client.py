# src/modules/vertex_search_client.py

from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
from src.config import settings, log

# TUS DATOS (Confirmados)
PROJECT_ID = "pida-ai-v20"
LOCATION = "global"
DATA_STORE_ID = "almacen-web-pida_1765039607916"

# --- CORRECCIÓN AQUÍ: La función se debe llamar 'search' ---
def search(query: str, num_results: int = 5) -> str:
    """
    Busca en Vertex AI Search y devuelve un STRING formateado.
    """
    try:
        client_options = (
            ClientOptions(api_endpoint=f"{LOCATION}-discoveryengine.googleapis.com")
            if LOCATION != "global" else None
        )
        client = discoveryengine.SearchServiceClient(client_options=client_options)

        serving_config = client.serving_config_path(
            project=PROJECT_ID,
            location=LOCATION,
            data_store=DATA_STORE_ID,
            serving_config="default_config",
        )

        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=num_results,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True
                )
            ),
        )

        response = client.search(request)
        
        if not response.results:
            log.warning(f"Vertex Search no encontró resultados para: {query}")
            return ""

        # Formatear resultados como texto para el LLM
        formatted_output = "\n\n### Información Jurídica Externa (Web):\n"
        
        for result in response.results:
            data = result.document.derived_struct_data
            
            # Extraer snippet
            snippet = ""
            if "snippets" in data and len(data["snippets"]) > 0:
                snippet = data["snippets"][0].get("snippet", "")
            if not snippet:
                snippet = data.get("pagemap", {}).get("metatags", [{}])[0].get("og:description", "")

            title = data.get("title", "Documento Legal")
            link = data.get("link", "#")

            formatted_output += f"- **Fuente:** [{title}]({link})\n"
            formatted_output += f"  > {snippet}\n\n"

        log.info(f"Vertex Search retornó {len(response.results)} docs.")
        return formatted_output

    except Exception as e:
        log.error(f"Error crítico en Vertex Search: {e}")
        return ""
