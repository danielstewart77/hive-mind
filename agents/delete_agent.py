import os
from typing import Generator
from agent_tooling import tool, discover_tools

@tool(tags=["agent"])
def delete_agent(agent_name: str) -> Generator[str, None, None]:
    """
    This function deletes an unwanted agent by the provided name.
    
    Args:
    - agent_name: The name of the agent to be deleted
    
    Returns:
    A message indicating the success or failure of the operation.
    """
    from agents.agent_read import get_agent_by_name

    try:
        # Locate the agent by name
        agent = get_agent_by_name(agent_name)

        if agent is None:
            return f"Agent named '{agent_name}' not found."

        # Get the file path of the agent
        file_path = agent.file_path
        
        if not os.path.exists(file_path):
            return f"Agent file path '{file_path}' does not exist. Deletion failed."

        # Delete the agent file ensuring no dependencies are left
        os.remove(file_path)

        yield f"Agent '{agent_name}' deleted successfully and system updated."

        discover_tools()

    except Exception as e:
        yield f"An error occurred during deletion: {e}"