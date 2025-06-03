# mcp_filesystem_server.py
import os
import sys
from typing import List
from agent_tooling import tool

from agents.file_editor import file_editor

ALLOWED_DIRS: List[str] = [
    os.path.abspath("/home/USER/Storage"),
]

def is_allowed(path: str) -> bool:
    """Check if the given path is within the allowed directories."""
    abs_path = os.path.abspath(path)
    return any(abs_path.startswith(d) for d in ALLOWED_DIRS)

@tool(tags=["triage"])
def list_allowed_directories() -> List[str]:
    """List directories that can be accessed."""
    return ALLOWED_DIRS

@tool(tags=["triage"])
def read_file(path: str) -> str:
    """Read the contents of a file."""
    if not is_allowed(path):
        raise ValueError("Access denied: Path is outside allowed directories.")
    with open(path, 'r') as file:
        return file.read()
    
@tool(tags=["triage"])
def create_file(path: str) -> str:
    """Create an empty file when the user specifically says create a NEW file."""
    if not is_allowed(path):
        raise ValueError("Access denied: Path is outside allowed directories.")
    
    if os.path.exists(path):
        return f"File already exists: {path}"
    
    # Ensure parent directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    return file_editor(path)
