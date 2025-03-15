from typing import Any, Dict, List, Tuple

from agent_tooling import get_tool_schemas, get_tool_function

def get_tools() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    functions = get_tool_schemas()

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
        available_functions[func_name] = get_tool_function(func_name)

    return tools, available_functions