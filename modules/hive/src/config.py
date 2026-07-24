import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Server settings
HOST = os.getenv("HIVE_HOST", "127.0.0.1")
PORT = int(os.getenv("HIVE_PORT", "8710"))

# Security settings. HIVE_SECRET_KEY signs session cookies; the terminal
# site must share this value (and the auth db) for one login to cover both.
SECRET_KEY = os.getenv("HIVE_SECRET_KEY", "change-this-in-production")
