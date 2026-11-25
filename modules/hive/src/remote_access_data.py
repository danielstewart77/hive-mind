"""The website's narrow client for the private host controller."""
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx


def root():
    return Path(os.getenv("HIVE_REMOTE_ACCESS_DIR", "/data/remote-access"))


def trusted(request):
    try:
        key = json.loads((root() / "verifier.key").read_text())["key"]
        return secrets.compare_digest(key.encode(), request.headers.get("x-hive-remote-key", "").encode())
    except (OSError, ValueError, KeyError):
        return False


async def request(method, path, body=None):
    try:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(root() / "control.sock")), timeout=85) as client:
            response = await client.request(method, "http://host" + path, json=body)
        data = response.json()
        if not response.is_success:
            raise ValueError(data.get("detail") or "The remote access operation failed")
        return data
    except (httpx.HTTPError, json.JSONDecodeError):
        raise ValueError("The host remote access controller is unavailable") from None


def user_path(username):
    return "/users/" + quote(username, safe="")
