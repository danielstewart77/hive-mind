from agent_tooling import tool
from py2neo import Graph, Node, Relationship
import os
import keyword

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
def add_article_to_neo4j_db(title: str, text: str, date: str):
    """
    Connects to a Neo4j database, creates an Article node with the
    provided title, text, and date. It extracts keywords from the title as topics, relates them to the article,
    and handles entities for further relational context.
    """
    neo4j_url = _get_credential("NEO4J_URL")
    neo4j_username = _get_credential("NEO4J_USERNAME")
    neo4j_password = _get_credential("NEO4J_PASSWORD")

    # Establish connection to the Neo4j database
    graph = Graph(neo4j_url, auth=(neo4j_username, neo4j_password))

    # Ensure date is a string if not provided correctly
    if not isinstance(date, str):
        date = str(date)

    # Create Article node
    article_node = Node("Article", title=title, text=text, date=date)
    graph.create(article_node)

    # Extract keywords from the title assuming keywords of more than 3 characters are topics
    title_keywords = [word for word in title.split() if len(word) > 3 and not keyword.iskeyword(word)]

    # Relate each keyword to the article as a Topic
    for kw in title_keywords:
        topic_node = Node("Topic", name=kw)
        graph.merge(topic_node, "Topic", "name") # Ensure no duplicate topic nodes
        rel = Relationship(topic_node, "RELATED_TO", article_node)
        graph.create(rel)

    print("Article and related topics added to Neo4j Database.")
