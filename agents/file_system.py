# mcp_filesystem_server.py
import os
import sys
from typing import List
from agent_tooling import tool

from agents.file_editor import file_editor

ALLOWED_DIRS: List[str] = [
    os.path.abspath("/home/USER/Storage/Dev"),
]

def is_allowed(path: str) -> bool:
    """Check if the given path is within the allowed directories."""
    abs_path = os.path.abspath(path)
    return any(abs_path.startswith(d) for d in ALLOWED_DIRS)

@tool(tags=["file"])
def list_allowed_directories() -> List[str]:
    """respond to the ls command with no specific directory."""
    entries = []

    for base_dir in ALLOWED_DIRS:
        for root, dirs, files in os.walk(base_dir):
            # Add the root directory
            entries.append(root)
            # Add all subdirectories
            for d in dirs:
                entries.append(os.path.join(root, d))
            # Add all files
            for f in files:
                entries.append(os.path.join(root, f))
    
    return entries

@tool(tags=["file"])
def list_directory_contents(path: str) -> List[str]:
    """
    respond to the ls command and List the contents (files and directories) of a given folder.
    You can specify an absolute path or a folder relative to an allowed directory.
    """
    # Expand relative paths under the first allowed base path
    if not os.path.isabs(path):
        path = os.path.join(ALLOWED_DIRS[0], path)

    abs_path = os.path.abspath(path)

    if not is_allowed(abs_path):
        raise ValueError(f"Access denied: {abs_path} is outside the allowed directories.")

    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"The directory {abs_path} does not exist.")

    if not os.path.isdir(abs_path):
        raise NotADirectoryError(f"{abs_path} is not a directory.")

    # List the contents (files + directories)
    entries = os.listdir(abs_path)
    return [os.path.join(abs_path, entry) for entry in entries]


@tool(tags=["file"])
def read_file(path: str) -> str:
    """Read the contents of a file."""
    if not is_allowed(path):
        raise ValueError("Access denied: Path is outside allowed directories.")
    with open(path, 'r') as file:
        return file.read()

@tool(tags=["file"])
def create_file(path: str) -> str:
    """Create an empty file when the user specifically says create a NEW file."""
    if not is_allowed(path):
        raise ValueError("Access denied: Path is outside allowed directories.")
    
    if os.path.exists(path):
        return f"File already exists: {path}"
    
    # Ensure parent directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    return file_editor(path)
