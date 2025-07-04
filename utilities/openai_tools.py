import os
import json
from typing import Generator
from openai import OpenAI
from dotenv import load_dotenv
from agent_tooling import tool
from pydantic import BaseModel
from models.open_web_ui import Autocompletion, Tags, Summary

load_dotenv(dotenv_path='secrets.env')
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

def completions(message: str, model: str = "gpt-4.1") -> str:
    """Call the OpenAI API and return the raw response."""
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "developer", "content": message}
        ]
    )
    content = completion.choices[0].message.content
    return content

def completions_with_messages(messages: list[dict[str, str]], model: str = "gpt-4.1") -> str:
    """Call the OpenAI API and return the raw response."""
    completion = client.chat.completions.create(
        model=model,
        messages=messages
    )
    content = completion.choices[0].message.content
    return content

from typing import Type
from pydantic import BaseModel

def completions_structured(
    message: str,
    response_format: type[BaseModel],  # Accepts any BaseModel subclass
    model: str = "gpt-4.1"
) -> BaseModel:
    """Call the OpenAI API and return the parsed response as the given BaseModel subclass."""
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "developer", "content": message}
        ],
        response_format=response_format
    )
    content = completion.choices[0].message.parsed
    if content is None:
        raise ValueError("OpenAI API did not return a valid parsed response.")
    return content

def completions_streaming(message: str, model: str = "gpt-4o") -> Generator[str, None, None]:
    """Call the OpenAI API for streaming output."""
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "developer", "content": message}
        ],
        stream=True
    )

    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content is not None:
            yield chunk.choices[0].delta.content

# completions streaming with messages
def completions_streaming_with_messages(messages: list[dict[str, str]], model: str = "gpt-4.1") -> Generator[str, None, None]:
    """Call the OpenAI API for streaming output."""
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True
    )

    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content is not None:
            yield chunk.choices[0].delta.content
    
#     @tool
# def openai_embeddings(api_key: str, model: str, prompt: str):
#     """Call the OpenAI API for embeddings."""
#     response = openai.Embedding.create(
#         input=prompt,
#         model=model
#     )
#     return response.data