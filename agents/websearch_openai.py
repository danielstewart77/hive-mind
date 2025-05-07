import os
from typing import Generator
from dotenv import load_dotenv
from openai import OpenAI
from agent_tooling import tool

from utilities.messages import get_last_user_message

load_dotenv(dotenv_path='secrets.env')
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

@tool(tags=["triage"])
def web_search(messages: list[dict[str, str]]) -> Generator[str, None, None]:
    """Use this agent to perform a web search and return the results. This is useful for finding information that is not in the knowledge base."""

    input = get_last_user_message(messages)
    stream = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search_preview"}],
        input=input,
        stream=True
    ).output_text

    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content is not None:
            yield chunk.choices[0].delta.content