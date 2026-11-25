"""Configuration constants for HiveTools."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = os.getenv("HT_SECRET_KEY", "change-this-in-production")

# Generated once per process — used to authenticate internal HITL replay calls
INTERNAL_SECRET = secrets.token_hex(32)
