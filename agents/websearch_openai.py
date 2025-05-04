import os
from dotenv import load_dotenv
from openai import OpenAI
from agent_tooling import tool

load_dotenv(dotenv_path='secrets.env')
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

@tool["triage"]
def web_search(question: str) -> str:
    response = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search_preview"}],
        input=question
    ).output_text

    return response