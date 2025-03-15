import os
import json
from openai import OpenAI
from dotenv import load_dotenv
from agent_tooling import tool
from pydantic import BaseModel
from models.open_web_ui import Autocompletion, Tags, Summary

load_dotenv(dotenv_path='secrets.env')
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

#@tool
def add_numbers(a: int, b: int) -> str:
    """Adds two numbers together and returns the result as a standardized response."""
    sum_val = a + b
    result = f"The sum of {a} and {b} is {sum_val}."
    return result

#@tool
def message_autocomplete(message: str) -> dict:
    """Suggests autocompletions for the given message."""
    raw_response = api_structure_output(message=message, model=Autocompletion)
    return raw_response

#@tool
def create_title_and_emoji(message: str) -> dict:
    """Creates a title for the chat thread with an emoji."""
    raw_response = api_structure_output(message=message, model=Summary)
    return raw_response

#@tool
def create_tags(message: str) -> dict:
    """Creates tags categorizing the main themes of the chat history."""
    raw_response = api_structure_output(message=message, model=Tags)
    return raw_response

def api(message: str) -> str:
    """Call the OpenAI API and return the raw response."""
    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "developer", "content": message}
        ]
    )
    content = completion.choices[0].message.content
    return content

def api_structure_output(message: str, model: BaseModel) -> str:
    """Call the OpenAI API and return the raw response."""
    completion = client.beta.chat.completions.parse(
        model="gpt-4o-2024-08-06",
        messages=[
            {"role": "developer", "content": message}
        ],
        response_format=model
    )
    content = completion.choices[0].message.content
    return content