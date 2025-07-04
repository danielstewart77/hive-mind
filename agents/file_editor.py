import os
from agent_tooling import tool
from utilities.messages import get_last_user_message
from shared.state import set_editor_state

@tool(tags=["code"])
def file_editor(file_path: str) -> str:
    """Call this function when the user wants to open/edit/modify a file."""

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"{file_path} does not exist")

    # Set shared editor state
    set_editor_state({"file_path": file_path})

    return """@editor <p><strong>Editor Ready:</strong> <a href="http://203.0.113.10:7779/edit" target="_blank">Open File Editor</a></p>"""
