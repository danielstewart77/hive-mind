import html
import os
from agent_tooling import tool
from utilities.messages import get_last_user_message
from utilities.editor_bridge import editor_state

@tool(tags=["triage", "files"])
def file_editor(file_path: str, messages: list[dict[str, str]]) -> str:
    """Call this function when the user wants to open/edit/modify a file."""

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"{file_path} does not exist")

    editor_state["file_path"] = file_path

    return """@editor <p><strong>Editor Ready:</strong> <a href="http://203.0.113.10:8000/edit" target="_blank">Open File Editor</a></p>"""