import os
from dotenv import load_dotenv
from openai import OpenAI
from agent_tooling import tool

from utilities.messages import get_last_user_message

load_dotenv(dotenv_path='secrets.env')
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

@tool(tags=["triage"])
def web_search(question: str, messages: list[dict[str, str]]) -> str:
    """Use this agent to perform a web search and return the results. This is useful for finding information that is not in the knowledge base."""

    input = get_last_user_message(messages)
    response = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search_preview"}],
        input=input
    ).output_text

    return response