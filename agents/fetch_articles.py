import json
from agent_tooling import tool
from agents.secret_manager import get_credential
from neo4j import GraphDatabase


@tool(tags=["news"])
def fetch_articles(criteria: str) -> str:
    """Retrieve articles from Neo4j database matching the given criteria.

    Args:
        criteria: The criteria value to match against Article nodes.

    Returns:
        JSON string with list of articles (title + content) or error message.
    """
    uri = get_credential("NEO4J_URI")
    username = get_credential("NEO4J_USERNAME")
    password = get_credential("NEO4J_PASSWORD")

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
