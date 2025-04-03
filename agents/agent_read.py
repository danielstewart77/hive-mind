import json
from typing import Generator
from agent_tooling import tool, get_agents, Agent
from agents.openai import completions_streaming, completions_structured
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

@tool
def list_agents() -> Generator[str, None, None]: 
    """
    List all available agents in the system. This function retrieves the list of agents and formats them into a readable string.
    
    Returns:
        str: A formatted string listing all available agents.
    """

    agents = get_agents()

    agent_names = [agent.name for agent in agents]

    agent_descriptions = completions_streaming(
            message=f'''Use these agent names: {json.dumps(agent_names)} 
            to list the agents of the HIVE MIND'''
        )

    # stream the response
    for chunk in agent_descriptions:
        yield chunk

@tool
def get_agent_by_name(name: str) -> Agent:
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

@tool
def get_agent_by_description(description: str) -> Agent:
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

@tool
def get_agent_code_by_name(name: str) -> Generator[str, None, None]: 
    """
    Get the code of an agent by its name. This function searches for an agent with the specified name and returns its code.

    Args:
        name (str): The name of the agent to search for.

    Returns:
        Generator[str, None, None]:  The code of the matched agent.
    """
    # Retrieve the list of agents
    # list[Agent]
    agents = get_agents()

    agent_names = [agent.name for agent in agents]

    matches = completions_structured(
        message=f"Of these agents names: {agent_names} find an agent with the name: {name}. if no exact match, return similar names.",
        response_format=AgentMatches
    )

    if not matches:
        stream = completions_streaming(
            message=f"Tell the user that no  angents were found with the name: {name}. Nor were there any similar names."
        )
    if len(matches.agents) > 1:
        stream = completions_streaming(
            message=f"Tell the user that no agents were found with the name: {name}, but agents were found with a similar name: {[agent.name for agent in matches]}"
        )
    if len(matches.agents) == 0:
        stream = completions_streaming(
            message=f"Tell the user that no  angents were found with the name: {name}. Nor were there any similar names."
        )

    # select the agent from the list of agents by name
    agent = next((agent for agent in agents if agent.name == name))
    code = agent.code

    # If exactly one agent is found, return its code
    stream = completions_streaming(
        message=f"Return this code: {code} for the agent with the name: {name} along with nicely formatted markdown explaining the code."
    )

    # stream the response
    for chunk in stream:
        yield chunk

@tool       
def get_agent_code_by_description(description: str) -> Generator[str, None, None]: 
    """
    Get the code of an agent by its description. This function searches for an agent with the specified description and returns its code.

    Args:
        description (str): The description of the agent to search for.

    Returns:
        Generator[str, None, None]: The code of the matched agent.
    """

    agents = get_agents()

    agent_descriptions = [f"{agent.name}: {agent.description}"  for agent in agents]

    matches = completions_structured(
        message=f"Of these agents descriptions: {agent_descriptions} find an agent(s) with the description: {description}",
        response_format=AgentMatches
    )

    if not matches:
        stream = completions_streaming(
            message=f"Tell the user that no  angents were found with a similar description."
        )
    if len(matches.agents) > 1:
        stream = completions_streaming(
            message=f"Tell the user that there were multiple agents found with a similar description, and return their names and descriptions: {[f'{agent.name}: {agent.description}' for agent in matches.agents]}"
        )
    if len(matches.agents) == 0:
        stream = completions_streaming(
            message=f"Tell the user that no  angents were found with a similar description."
        )
    # If exactly one agent is found, return its code
    # get the agent code from the agents list
    # select the agent from the list of agents by name
    agent = next((agent for agent in agents if agent.name == matches.agents[0].name), None)

    code = agent.code
    stream = completions_streaming(
        message=f"Return this code: {code} for the agent with the name: {matches.agents[0].name} along with nicely formatted markdown explaining the code."
    )

    # stream the response
    for chunk in stream:
        yield chunk

@tool
def get_agents_with_descriptions() -> Generator[str, None, None]:
    """
    Describes to the user what the agents of the HIVE MIND can do. This function retrieves the list of agents and formats them into a readable string.
    The function streams the agent information to the user.
    Args:
        None
    Yields:
        Generator[str, None, None]:  A formatted string listing all available agents with their descriptions.
    """

    # Retrieve the list of agents
    agent_info = [{"name": agent.name, "description": agent.description} for agent in _agents]

    # Stream the agent information
    agent_descriptions = completions_streaming(
        message=f'''Use these agent names and descriptions: {json.dumps(agent_info)} 
        to describe the abilities of the HIVE MIND'''
    )

    # stream the response
    for chunk in agent_descriptions:
        yield chunk