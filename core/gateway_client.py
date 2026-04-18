"""
Shared gateway client and utilities for HiveMind bots.

Centralises all gateway HTTP logic so Discord and Telegram bots
stay thin and don't duplicate code.
"""

import asyncio
import glob
import json
import os
import re
from datetime import datetime
from typing import AsyncGenerator

import aiohttp

_SKILLS_DIR = os.path.expanduser("~/.claude/skills")
_locks: dict[int, asyncio.Lock] = {}
_chat_queues: dict[int, asyncio.Queue] = {}


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def get_skills() -> list[dict]:
    """Read all user-invocable skills from SKILL.md files."""
    skills = []
    for path in sorted(glob.glob(os.path.join(_SKILLS_DIR, "*/SKILL.md"))):
        try:
            with open(path) as f:
                content = f.read()
            m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not m:
                continue
            fm: dict[str, str] = {}
            for line in m.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip().strip('"').strip("'")
            invocable = fm.get("user-invocable", fm.get("user_invocable", "")).lower()
            if invocable == "true" and (name := fm.get("name", "")):
                skills.append({
                    "name": name,
                    "description": fm.get("description", "")[:100],
                    "argument_hint": fm.get("argument-hint", ""),
                })
        except Exception:
            pass
    return skills


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def get_queue(chat_id: int) -> asyncio.Queue:
    if chat_id not in _chat_queues:
        _chat_queues[chat_id] = asyncio.Queue()
    return _chat_queues[chat_id]


def time_ago(ts: float) -> str:
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)} min ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


# ---------------------------------------------------------------------------
# Gateway client
# ---------------------------------------------------------------------------

class GatewayClient:
    """HTTP client for the Hive Mind gateway server."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        server_url: str,
        owner_type: str,
        surface_prompt: str | None = None,
        mind_id: str = "ada",
    ):
        self.http = http
        self.server_url = server_url
        self.owner_type = owner_type
        self.surface_prompt = surface_prompt
        self.mind_id = mind_id

    async def ensure_session(self, user_id: int, client_ref: int | str) -> str:
        """Get active session for this client, or create one."""
        async with self.http.get(
            f"{self.server_url}/sessions",
            params={"client_type": self.owner_type, "client_ref": str(client_ref)},
        ) as resp:
            data = await resp.json()
            for s in data:
                if s.get("is_active"):
                    return s["id"]

        payload: dict = {
            "owner_type": self.owner_type,
            "owner_ref": str(user_id),
            "client_ref": str(client_ref),
            "mind_id": self.mind_id,
        }
        if self.surface_prompt:
            payload["surface_prompt"] = self.surface_prompt
        async with self.http.post(f"{self.server_url}/sessions", json=payload) as resp:
            return (await resp.json())["id"]

    async def server_command(
        self, user_id: int, client_ref: int | str, content: str
    ) -> dict:
        """Send a server command and return the JSON response."""
        async with self.http.post(
            f"{self.server_url}/command",
            json={
                "content": content,
                "owner_type": self.owner_type,
                "owner_ref": str(user_id),
                "client_ref": str(client_ref),
                "mind_id": self.mind_id,
            },
        ) as resp:
            return await resp.json()

    async def query_stream(
        self, user_id: int, client_ref: int | str, prompt: str,
        images: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Yield assistant text chunks from the gateway SSE response as they arrive.

        Yields each assistant message block as it comes in, enabling callers
        to update a live message progressively rather than waiting for the full
        response.  Falls back to the result event text if no assistant blocks
        were received (e.g. tool-only turns).
        """
        session_id = await self.ensure_session(user_id, client_ref)
        yielded_any = False
        result_fallback = ""

        # SSE streams can be very long-lived (HITL approval waits, docker
        # builds, etc.), so override the default aiohttp timeouts.
        sse_timeout = aiohttp.ClientTimeout(total=0, sock_read=0)
        payload = {"content": prompt}
        if images:
            payload["images"] = images
        async with self.http.post(
            f"{self.server_url}/sessions/{session_id}/message",
            json=payload,
            timeout=sse_timeout,
        ) as resp:
            buf = ""
            async for chunk in resp.content.iter_any():
                buf += chunk.decode()
                while "\n" in buf:
                    raw_line, buf = buf.split("\n", 1)
                    raw_line = raw_line.strip()
                    if not raw_line or not raw_line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(raw_line.removeprefix("data: "))
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text" and block.get("text"):
                                yield block["text"]
                                yielded_any = True
                    elif etype == "result":
                        result_fallback = event.get("result", "")

        if not yielded_any and result_fallback:
            yield result_fallback

    async def query(self, user_id: int, client_ref: int | str, prompt: str,
                    images: list[dict] | None = None) -> str:
        """Send a query and return the complete response text (non-streaming)."""
        texts: list[str] = []
        async for text in self.query_stream(user_id, client_ref, prompt, images=images):
            texts.append(text)
        return "\n\n".join(texts) or "(No response)"
