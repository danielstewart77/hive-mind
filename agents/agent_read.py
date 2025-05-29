import json
from typing import Generator
from agent_tooling import discover_tools, tool, get_agents, Agent
from utilities.openai_tools import completions_streaming, completions_structured
from pydantic import BaseModel

class AgentMatches(BaseModel):
    agents: list[Agent]
    class Config:
        extra = "forbid"

_agents = get_agents()

# Custom JSON encoder for Agent
class AgentEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Agent):
            return {
                'name': obj.name,
                'description': obj.description,
                'file_name': obj.file_name,
                'file_path': obj.file_path,
                'code': obj.code
            }
        return super().default(obj)
    

@tool(tags=["agent", "editor"])
def get_agents_with_descriptions(messages: list[dict[str, str]]) -> Generator[str, None, None]:
    """
    Describes to the user what the agents of the HIVE MIND can do. This function retrieves the list of agents and formats them into a readable string.
    The function streams the agent information to the user.
    Args:
        None
    Yields:
        Generator[str, None, None]:  A formatted string listing all available agents with their descriptions.
    """

    discover_tools()

    agents = get_agents()


    # Retrieve the list of agents
    agent_info = [{"name": agent.name, "description": agent.description, "file_name": agent.file_name, "file_path": agent.file_path} for agent in agents]

    # Stream the agent information
    agent_descriptions = completions_streaming(
        message=f'''
        For each agent of the HIVE MIND, list all of the information:
        1) agent's name, 
        2) parameters, 
        3) file name, 
        4) file path, and 
        5) give a brief description: {json.dumps(agent_info)}'''
    )

    # stream the response
    for chunk in agent_descriptions:
        yield chunk

#@tool
def get_agent_by_name(name: str, messages: list[dict[str, str]] = None) -> Agent:
    """
    Get an agent by its name. This function searches for an agent with the specified name and returns it.

    Args:
        name (str): The name of the agent to search for.

    Returns:
        AgentMatches: A list containing the matched agents.
    """

    json_agents = json.dumps(_agents, cls=AgentEncoder)
    matches = completions_structured(
        message=f"Of these agents: {json_agents} return the agent with the name: {name}",
        response_format=AgentMatches
    )
    if not matches:
        return None
    if len(matches.agents) > 1:
        raise ValueError("Multiple agents found with the same name.")
    if len(matches.agents) == 0:
        raise ValueError("No agent found with the given name.")
    # If exactly one agent is found, return it
    return matches.agents[0]

#@tool
def get_agent_by_description(description: str, messages: list[dict[str, str]] = None) -> Agent:
    """
    Get an agent by its description. This function searches for an agent with the specified description and returns it.

    Args:
        description (str): The description of the agent to search for.

    Returns:
        AgentMatches: A list containing the matched agents.
    """

    json_agents = json.dumps(_agents, cls=AgentEncoder)
    matches = completions_structured(
        message=f"Of these agents: {json_agents} ind an agent with the description: {description}",
        response_format=AgentMatches
    )
    if not matches:
        return None
    if len(matches.agents) > 1:
        raise ValueError("Multiple agents found with the same description.")
    if len(matches.agents) == 0:
        raise ValueError("No agent found with the given description.")
    # If exactly one agent is found, return it
    return matches.agents[0]

@tool(tags=["agent"])
def get_agent_code_by_name(name: str, messages: list[dict[str, str]] = None) -> Generator[str, None, None]:
    """
    Get the code of an agent by its name. This function searches for an agent with the specified name and returns its code.

    Args:
        name (str): The name of the agent to search for.

    Returns:
        Generator[str, None, None]:  The code of the matched agent.
    """
    # Retrieve the list of agents
    # list[Agent]
    discover_tools()

    agents = get_agents()

    agent_names = [agent.name for agent in agents]

    matches = completions_structured(
        message=f"Of these agents names: {agent_names} find an agent with the name: {name}. if no exact match, return similar names.",
        response_format=AgentMatches
    )

    if not matches:
        stream = completions_streaming(
            message=f"Tell the user that no agents were found with the name: {name}. Nor were there any similar names."
        )
    if len(matches.agents) > 1:
        stream = completions_streaming(
            message=f"Tell the user that no agents were found with the name: {name}, but agents were found with a similar name: {[agent.name for agent in matches.agents]}"
        )
    if len(matches.agents) == 0:
        stream = completions_streaming(
            message=f"Tell the user that no agents were found with the name: {name}. Nor were there any similar names."
        )

    # select the agent from the list of agents by name
    agent = next((agent for agent in agents if agent.name == name))
    code = agent.code

    # If exactly one agent is found, return its code
    stream = completions_streaming(
        message=f'''Return ALL of this code: {code} in a (python markdown box) for the agent with the name: {name} 
        and at the bottom, an explanation of the code in nicely formatted markdown.'''
    )

    # stream the response
    for chunk in stream:
        yield chunk

@tool(tags=["agent"])
def get_agent_code_by_name(name: str, messages: list[dict[str, str]] = None) -> Generator[str, None, None]:
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

    if not matches.agents:
        stream = completions_streaming(
            message=f"No agents found with the name: {name}, nor any similar names."
        )
        for chunk in stream:
            yield chunk
        return

    if len(matches.agents) > 1:
        similar_names = [agent.name for agent in matches.agents]
        stream = completions_streaming(
            message=f"No exact match found for '{name}', but similar agent names include: {similar_names}"
        )
        for chunk in stream:
            yield chunk
        return

    agent = next((agent for agent in agents if agent.name == name), None)
    if not agent:
        stream = completions_streaming(
            message=f"The agent named '{name}' was matched in name but could not be found in the agent list."
        )
        for chunk in stream:
            yield chunk
        return

    stream = completions_streaming(
        message=f'''Return ALL of this code: {agent.code} in a (python markdown box) for the agent with the name: {name},
        and at the bottom, an explanation of the code in nicely formatted markdown.'''
    )
    for chunk in stream:
        yield chunk