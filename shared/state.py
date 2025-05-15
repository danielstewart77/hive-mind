# shared/state.py
from typing import Generator

stream_cache: dict[str, Generator] = {}
