import html
import os
from agent_tooling import tool
from utilities.messages import get_last_user_message
from shared.state import editor_state
import json

@tool(tags=["triage", "files"])
def file_editor(file_path: str) -> str:
    """Call this function when the user wants to open/edit/modify a file."""

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"{file_path} does not exist")

    # Persist to disk
    with open("/tmp/editor_state.json", "w") as f:
        json.dump({"file_path": file_path}, f)

    return """@editor <p><strong>Editor Ready:</strong> <a href="http://0.0.0.0:7779/edit" target="_blank">Open File Editor</a></p>"""