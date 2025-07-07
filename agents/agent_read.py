import json
from typing import Optional
from agent_tooling import discover_tools, tool, get_agents, Agent
from utilities.messages import get_last_user_message
from utilities.openai_tools import completions_streaming, completions_structured
from pydantic import BaseModel

class AgentMatches(BaseModel):
    agents: list[Agent]
    class Config:
        extra = "forbid"

_agents = get_agents()

# Custom JSON encoder for Agent
class AgentEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Agent):
            return {
                'name': o.name,
                'description': o.description,
                'file_name': o.file_name,
                'file_path': o.file_path,
                'code': o.code
            }
        return super().default(o)
    

@tool(tags=["agent"])
def get_agents_with_descriptions(messages: list[dict[str, str]]) -> str:
    """
    Describes to the user what the agents of the HIVE MIND can do. This function retrieves the list of agents and formats them into a readable string.
    Args:
        None
    Returns:
        str: A formatted string listing all available agents with their descriptions.
    """

    discover_tools()

    agents = get_agents()

    # Retrieve the list of agents
    agent_info = [{"name": agent.name, "description": agent.description, "file_name": agent.file_name, "file_path": agent.file_path} for agent in agents]

    if not messages:
        return json.dumps(agent_info, indent=2)

    else:
        message = get_last_user_message(messages)
        if not message:
            message = f'''
            For each agent of the HIVE MIND, list all of the information:
            1) agent's name, 
            2) parameters, 
            3) file name, 
            4) file path, and 
            5) give a brief description'''

        # Get the agent information as a complete response
        agent_descriptions = completions_streaming(
            message=f'{message}: {json.dumps(agent_info)}'
        )

        # Collect all chunks into a single string
        full_response = ""
        for chunk in agent_descriptions:
            full_response += chunk
        
        return full_response

#@tool
def get_agent_by_name(name: str, messages: Optional[list[dict[str, str]]] = None) -> Optional[Agent]:
    """
    Get an agent by its name. This function searches for an agent with the specified name and returns it.

    Args:
        name (str): The name of the agent to search for.

    Returns:
        Optional[Agent]: The matched agent or None if not found.
    """

    json_agents = json.dumps(_agents, cls=AgentEncoder)
    matches = completions_structured(
        message=f"Of these agents: {json_agents} return the agent with the name: {name}",
        response_format=AgentMatches
    )
    matches = AgentMatches(**matches.model_dump()) if matches else None
    if not matches:
        return None
    if len(matches.agents) > 1:
        raise ValueError("Multiple agents found with the same name.")
    if len(matches.agents) == 0:
        raise ValueError("No agent found with the given name.")
    # If exactly one agent is found, return it
    return matches.agents[0]

#@tool
def get_agent_by_description(description: str, messages: Optional[list[dict[str, str]]] = None) -> Optional[Agent]:
    """
    Get an agent by its description. This function searches for an agent with the specified description and returns it.

    Args:
        description (str): The description of the agent to search for.

    Returns:
        Optional[Agent]: The matched agent or None if not found.
    """

    json_agents = json.dumps(_agents, cls=AgentEncoder)
    matches = completions_structured(
        message=f"Of these agents: {json_agents} ind an agent with the description: {description}",
        response_format=AgentMatches
    )
    matches = AgentMatches(**matches.model_dump()) if matches else None
    if not matches:
        return None
    if len(matches.agents) > 1:
        raise ValueError("Multiple agents found with the same description.")
    if len(matches.agents) == 0:
        raise ValueError("No agent found with the given description.")
    # If exactly one agent is found, return it
    return matches.agents[0]

@tool(tags=["agent"])
def get_agent_code_by_name(name: str, messages: Optional[list[dict[str, str]]] = None) -> str:
    """
    Get the code of an agent by its name. This function searches for an agent with the specified name and returns its code.
    """
    discover_tools()
    agents = get_agents()
    agent_names = [agent.name for agent in agents]

    matches = completions_structured(
        message=f"Of these agent names: {agent_names}, find one matching '{name}'. If no exact match, return similar names.",
        response_format=AgentMatches
    )
    matches = AgentMatches(**matches.model_dump()) if matches else None

    if not matches or not matches.agents:
        stream = completions_streaming(
            message=f"No agents found with the name: {name}, nor any similar names."
        )
        full_response = ""
        for chunk in stream:
            full_response += chunk
        return full_response

    if len(matches.agents) > 1:
        similar_names = [agent.name for agent in matches.agents]
        stream = completions_streaming(
            message=f"No exact match found for '{name}', but similar agent names include: {similar_names}"
        )
        full_response = ""
        for chunk in stream:
            full_response += chunk
        return full_response

    agent = next((agent for agent in agents if agent.name == name), None)
    if not agent:
        stream = completions_streaming(
            message=f"The agent named '{name}' was matched in name but could not be found in the agent list."
        )
        full_response = ""
        for chunk in stream:
            full_response += chunk
        return full_response

    stream = completions_streaming(
        message=f'''Return ALL of this code: {agent.code} in a (python markdown box) for the agent with the name: {name},
        and at the bottom, an explanation of the code in nicely formatted markdown.'''
    )
    full_response = ""
    for chunk in stream:
        full_response += chunk
    return full_response