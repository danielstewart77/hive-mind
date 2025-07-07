import os
import json
from typing import Generator
import ollama
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Type

load_dotenv(dotenv_path='secrets.env')

# Get Ollama host from environment variable, default to localhost
OLLAMA_HOST = os.getenv('OLLAMA_HOST', 'http://localhost:11434')

# Configure ollama client
client = ollama.Client(host=OLLAMA_HOST)

def completions(message: str, model: str = "granite3.3:8b") -> str:
    """Call the Ollama API and return the raw response."""
    response = client.chat(
        model=model,
        messages=[
            {"role": "user", "content": message}
        ]
    )
    content = response['message']['content']
    return content

def completions_with_messages(messages: list[dict[str, str]], model: str = "granite3.3:8b") -> str:
    """Call the Ollama API and return the raw response."""
    # Convert messages to Ollama format if needed
    ollama_messages = []
    for msg in messages:
        # Map OpenAI roles to Ollama roles
        role = msg.get('role', 'user')
        if role == 'developer':
            role = 'user'
        ollama_messages.append({
            "role": role,
            "content": msg.get('content', '')
        })
    
    response = client.chat(
        model=model,
        messages=ollama_messages
    )
    content = response['message']['content']
    return content

def completions_structured(
    message: str,
    response_format: type[BaseModel],  # Accepts any BaseModel subclass
    model: str = "granite3.3:8b"
) -> BaseModel:
    """Call the Ollama API and return the parsed response as the given BaseModel subclass."""
    # Create a prompt that instructs the model to respond in JSON format
    schema = response_format.model_json_schema()
    structured_prompt = f"""Please respond with a valid JSON object that matches this schema:

Schema: {json.dumps(schema, indent=2)}

User request: {message}

Respond ONLY with valid JSON, no additional text or formatting."""

    response = client.chat(
        model=model,
        messages=[
            {"role": "user", "content": structured_prompt}
        ],
        format="json"  # Request JSON format from Ollama
    )
    
    content = response['message']['content']
    
    try:
        # Parse the JSON response
        parsed_json = json.loads(content)
        # Create the Pydantic model instance
        parsed_response = response_format.model_validate(parsed_json)
        return parsed_response
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Ollama API did not return a valid JSON response: {e}")

def completions_streaming(message: str, model: str = "granite3.3:8b") -> Generator[str, None, None]:
    """Call the Ollama API for streaming output."""
    stream = client.chat(
        model=model,
        messages=[
            {"role": "user", "content": message}
        ],
        stream=True
    )
    
    for chunk in stream:
        if 'message' in chunk and 'content' in chunk['message']:
            yield chunk['message']['content']

def completions_streaming_with_messages(messages: list[dict[str, str]], model: str = "granite3.3:8b") -> Generator[str, None, None]:
    """Call the Ollama API for streaming output."""
    # Convert messages to Ollama format if needed
    ollama_messages = []
    for msg in messages:
        # Map OpenAI roles to Ollama roles
        role = msg.get('role', 'user')
        if role == 'developer':
            role = 'user'
        ollama_messages.append({
            "role": role,
            "content": msg.get('content', '')
        })
    
    stream = client.chat(
        model=model,
        messages=ollama_messages,
        stream=True
    )
    
    for chunk in stream:
        if 'message' in chunk and 'content' in chunk['message']:
            yield chunk['message']['content']

def list_models():
    """List available Ollama models."""
    try:
        models = client.list()
        return [model['name'] for model in models['models']]
    except Exception as e:
        print(f"Error listing models: {e}")
        return []

def pull_model(model_name: str):
    """Pull/download a model to Ollama."""
    try:
        client.pull(model_name)
        return f"Successfully pulled model: {model_name}"
    except Exception as e:
        return f"Error pulling model {model_name}: {e}"
