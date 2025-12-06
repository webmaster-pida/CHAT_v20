# src/modules/vertex_search_client.py

from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
from src.config import settings, log

def search(query: str, num_results: int = 5) -> str:
    """
    Busca en Vertex AI Search y devuelve un STRING formateado.
    Usa configuración segura desde src.config.
    """
    try:
        project_id = settings.VERTEX_SEARCH_PROJECT_ID
        location = settings.VERTEX_SEARCH_LOCATION
        data_store_id = settings.VERTEX_SEARCH_DATA_STORE_ID

        client_options = (
            ClientOptions(api_endpoint=f"{location}-discoveryengine.googleapis.com")
            if location != "global" else None
        )
        client = discoveryengine.SearchServiceClient(client_options=client_options)

        serving_config = client.serving_config_path(
            project=project_id,
            location=location,
            data_store=data_store_id,
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
