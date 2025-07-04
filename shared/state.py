import json
import os

from typing import Any, Dict, Generator, Optional

stream_cache: dict[str, Generator] = {}

workflow_id: Optional[str] = None

workflows: Dict[str, Dict[str, Any]] = {}

STATE_FILE = "/tmp/editor_state.json"

def get_editor_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass  # Optionally log this
    return {"file_path": None}

def set_editor_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
