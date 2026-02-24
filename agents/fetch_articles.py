import json
import os
from agent_tooling import tool
from neo4j import GraphDatabase

try:
    import keyring
except ImportError:
    keyring = None

_SERVICE_NAME = "hive-mind"


def _get_credential(key: str) -> str | None:
    """Get a credential from keyring, falling back to environment."""
    if keyring:
        val = keyring.get_password(_SERVICE_NAME, key)
        if val:
            return val
    return os.getenv(key)


@tool(tags=["news"])
def fetch_articles(criteria: str) -> str:
    """Retrieve articles from Neo4j database matching the given criteria.

    Args:
        criteria: The criteria value to match against Article nodes.

    Returns:
        JSON string with list of articles (title + content) or error message.
    """
    uri = _get_credential("NEO4J_URI")
    username = _get_credential("NEO4J_USERNAME") or os.getenv("USERNAME")
    password = _get_credential("NEO4J_PASSWORD") or os.getenv("PASSWORD")

    if not uri:
        return json.dumps({"error": "NEO4J_URI not configured in environment."})

    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (a:Article) WHERE a.criteria = $criteria RETURN a.title, a.content",
                criteria=criteria,
            )
            articles = [
                {"title": record["a.title"], "content": record["a.content"]}
                for record in result
            ]
        return json.dumps({"articles": articles, "count": len(articles)})
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        driver.close()
