import os
from typing import Any
from dotenv import load_dotenv
from openai import OpenAI
from agent_tooling import tool

from utilities.messages import get_last_user_message

load_dotenv()
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

@tool(tags=["web"])
def web_search(messages: list[dict[str, Any]]) -> str:
    """Use this agent to perform a web search and return the results. This is useful for finding information that is not in the knowledge base."""
    print("Searchin' up the interwebs...")

    response = client.chat.completions.create(
        model="gpt-4o-search-preview",
        messages=messages,  # type: ignore
        stream=False
    )

    return response.choices[0].message.content or "No search results found"