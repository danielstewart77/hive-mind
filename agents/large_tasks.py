import os
import re
import requests
import json
from typing import List, Dict, Union, Any
from pydantic import BaseModel
#from agent_tooling import tool
from dotenv import load_dotenv

from agents.ollama import ollama_generate

load_dotenv(dotenv_path='secrets.env')

OLLAMA_API_ADDRESS = os.getenv('OLLAMA_API_ADDRESS')
OLLAMA_API_URL = f'http://{OLLAMA_API_ADDRESS}:11434/api'

# Define Pydantic models for the task decomposition
class StepDescription(BaseModel):
    step: int
    description: List[str]

class TaskDecomposition(BaseModel):
    steps: List[StepDescription]


def decompose_task(message: str) -> TaskDecomposition:
    """Decompose a multi-step task into simple steps."""
    model = "deepseek-coder-v2:16b"
    # Schema for the expected JSON response
    schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "integer"},
                        "description": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["step", "description"]
                }
            }
        },
        "required": ["steps"]
    }
    schema_str = json.dumps(schema, indent=2)
    instruction = f'''Break this complex task down into its most simple,
    atomic steps. Recognize steps that must be done sequentially and steps
    that can be done in parallel - but make sure not to group all parallel 
    steps together when sequential steps are required between certain parallel
    steps. YOU MUST RESPOND IN JSON FORMAT FOLLOWING THIS SCHEMA:
    {schema_str}

    Example response format:
    {{
        "steps": [
            {{
                "step": 1,
                "description": ["action 1", "action 2"]
            }},
            {{
                "step": 2,
                "description": ["action 3", "action 4"]
            }}
        ]
    }}
    '''
    
    # call the Ollama API to decompose the task
    tasks = ollama_generate(message, model, instruction, TaskDecomposition)
    return tasks.model_dump_json()


def answer_is_complete(messages: list) -> bool:
    """Determine if the answer has been answered in full (even if the answer is that the LLM cannot answer it)."""
    class Answer(BaseModel):
        answer_is_complete: bool

        class Config:
            extra = "forbid"

    schema = {
        "type": "object",
        "properties": {
            "answer_is_complete": {
                "type": "boolean"
            }
        },
        "required": ["answer_is_complete"]
    }
    schema_str = json.dumps(schema, indent=2)
    instruction = f'''Determine if the answer is complete. An answer can be considered
    complete if it provides a full response to the question or if it indicates that the
    question cannot be answered. 
    
    YOU MUST RESPOND IN JSON FORMAT FOLLOWING THIS SCHEMA:
    {schema_str}
    
    Example response:
    {{
        "answer_is_complete": true
    }}
    
    DO NOT return the schema itself. Return an evaluation of completeness.
    '''
    
    # Convert the messages to a string if needed
    messages_str = json.dumps(messages) if isinstance(messages, list) else messages
    
    response = ollama_generate(
        message=messages_str, 
        llm_model="deepseek-coder-v2:16b", 
        instruction=instruction,
        data_model=Answer)
    
    # Clean up markdown if present
    if isinstance(response, Answer):
        try:
            return response.answer_is_complete
        except Exception as e:
            print(f"Error parsing response: {e}")
            print(f"Raw response: {response}")
            # Default to False if we can't parse
            return False
    else:
        # Default to False if we can't parse
        return False

#@tool
def largest_cryptocurrencies_by_market_cap(n: int) -> Dict[str, List[str]]:
    '''Returns the names of the n largest cryptocurrencies by market capitalization.'''
    return f"Agent Crypto Bro ₿😎💰: {['Bitcoin', 'Ethereum', 'XRP', 'Binance Coin', 'Solana', 'Cardano', 'Dogecoin', 'Tron', 'Avalanche', 'Shiba Inu'][:n]}"

