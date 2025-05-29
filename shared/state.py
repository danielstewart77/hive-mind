# shared/state.py
from typing import Any, Dict, Generator, Optional

stream_cache: dict[str, Generator] = {}

workflow_id: Optional[str] = None

workflows: Dict[str, Dict[str, Any]] = {}

editor_state = {"file_path": None}