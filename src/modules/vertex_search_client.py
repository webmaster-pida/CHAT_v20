# src/modules/vertex_search_client.py

from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
from src.config import settings, log

# Configuración fija de tu proyecto
PROJECT_ID = "pida-ai-v20"  # Tu proyecto
LOCATION = "global"         # La ubicación que elegiste (o us-central1)
DATA_STORE_ID = "almacen-web-pida_1765039607916" # El ID de la imagen

def search_legal_docs(query: str, num_results: int = 5):
    """
    Realiza una búsqueda semántica en Vertex AI Search (sitios jurídicos).
    Retorna una lista de diccionarios con título, enlace y fragmento.
    """
    try:
        # 1. Configurar cliente
        client_options = (
            ClientOptions(api_endpoint=f"{LOCATION}-discoveryengine.googleapis.com")
            if LOCATION != "global" else None
        )
        
        client = discoveryengine.SearchServiceClient(client_options=client_options)

        # 2. Definir dónde buscar (Serving Config)
        serving_config = client.serving_config_path(
            project=PROJECT_ID,
            location=LOCATION,
            data_store=DATA_STORE_ID,
            serving_config="default_config",
        )

        # 3. Preparar la solicitud
        # content_search_spec: Pide resúmenes extractivos (snippets)
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

        # 4. Ejecutar búsqueda
        response = client.search(request)

        # 5. Procesar resultados para que se parezcan a lo que usaba tu RAG
        results = []
        for result in response.results:
            data = result.document.derived_struct_data
            
            # Intentamos obtener el mejor fragmento de texto posible
            snippet = ""
            if "snippets" in data and len(data["snippets"]) > 0:
                snippet = data["snippets"][0].get("snippet", "")
            
            # Si no hay snippet, usamos la descripción o parte del contenido
            if not snippet:
                snippet = data.get("pagemap", {}).get("metatags", [{}])[0].get("og:description", "")

            results.append({
                "title": data.get("title", "Sin título"),
                "link": data.get("link", ""),
                "snippet": snippet,
                "source": "Vertex AI Legal Web"
            })

        log.info(f"Vertex Search encontró {len(results)} documentos para: '{query}'")
        return results

    except Exception as e:
        log.error(f"Error buscando en Vertex AI: {e}")
        return []

# Prueba rápida si ejecutas este archivo directo
if __name__ == "__main__":
    test_results = search_legal_docs("derechos laborales costa rica")
    for r in test_results:
        print(f"- {r['title']} ({r['link']})")
