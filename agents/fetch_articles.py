from agent_tooling import tool
from dotenv import load_dotenv
import os
from neo4j import GraphDatabase

@tool
def fetch_articles(criteria: str) -> str:
    """
    This function connects to a Neo4j database to retrieve articles based on specified
    criteria. It uses environment variables to securely store database credentials.
    The function formats the retrieved articles for easy reading and returns
    a success message if the operation completes successfully.
    """
    # Load environment variables
    load_dotenv(dotenv_path='secrets.env')
    NEO4J_URI = os.getenv('NEO4J_URI')
    USERNAME = os.getenv('USERNAME')
    PASSWORD = os.getenv('PASSWORD')

    # Verify and convert criteria to a usable format (string in this case)
    if not isinstance(criteria, str):
        raise ValueError("Criteria should be a string.")

    # Initialize the Neo4j driver
    driver = GraphDatabase.driver(NEO4J_URI, auth=(USERNAME, PASSWORD))

    try:
        with driver.session() as session:
            # Define a Cypher query to retrieve articles based on criteria
            query = """
            MATCH (a:Article)
            WHERE a.criteria = $criteria
            RETURN a.title, a.content
            """

            # Execute the query
            result = session.run(query, criteria=criteria)

            # Format the articles for easy reading
            articles = []
            for record in result:
                articles.append(f"Title: {record['a.title']}, Content: {record['a.content']}")

            # Print or log formatted articles (for illustrative purposes)
            for article in articles:
                print(article)

    except Exception as e:
        return f"An error occurred: {str(e)}"
    finally:
        driver.close()

    return "Articles retrieved and formatted successfully."