"""Graph data fetching from the hive_nervous_system lucent HTTP API.

Calls the /graph/data endpoint on the lucent service and returns nodes
and edges in a format suitable for Cytoscape.js visualization.
"""

import json
import urllib.request


def get_graph_data(api_url: str, limit: int = 400, bearer_token: str = "") -> dict:
    """Fetch graph data from the lucent /graph/data endpoint.

    Args:
        api_url: Base URL of lucent (e.g. http://hive-lucent:8424).
        limit: Maximum number of nodes to request.
        bearer_token: Optional bearer token attached as Authorization header.

    Returns:
        Dict with "nodes" and "edges" lists. On error, includes an
        "error" key and empty lists.
    """
    url = f"{api_url.rstrip('/')}/graph/data?limit={limit}"
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    return data
