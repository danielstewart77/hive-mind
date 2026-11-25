import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Server settings
HOST = os.getenv("TERMINAL_HOST", "127.0.0.1")
PORT = int(os.getenv("TERMINAL_PORT", "8711"))

