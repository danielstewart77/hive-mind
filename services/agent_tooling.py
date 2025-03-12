from typing import Any, Dict, List, Tuple

from agent_tooling import get_registered_tools

from agents.openai import add_numbers, message_autocomplete, create_title_and_emoji, create_tags, answer_question
from agents.large_tasks import decompose_task, largest_cryptocurrencies_by_market_cap, answer_is_complete
from agents.coingecko import current_crypto_price

def get_tools() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    functions = get_registered_tools()

    tools = []
    available_functions = {}

    for function in functions:
        tools.append({
            "type": "function",
            "function": {
                "name": function["name"],
                "description": function["description"],
                "parameters": function["parameters"],
                "return_type": function["return_type"],
            },
        })
        
        func_name = function["name"]
        available_functions[func_name] = globals().get(func_name)

    return tools, available_functions