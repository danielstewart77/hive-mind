"""LinkedIn FastAPI router -- migrated from hive_mind_mcp."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from auth_api import require_api_token
from hitl import _check_hitl
from schemas import LinkedInPostRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/linkedin", tags=["linkedin"])

_TOKEN_PATH = Path("/app/credentials/linkedin_token.json")
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"
_ASSETS_URL = "https://api.linkedin.com/v2/assets?action=registerUpload"


def _load_token() -> dict[str, Any] | None:
    if not _TOKEN_PATH.exists():
        return None
    try:
        return json.loads(_TOKEN_PATH.read_text())
    except Exception:
        log.exception("Failed to load LinkedIn token")
        return None


def _save_token(token: dict[str, Any]) -> None:
    _TOKEN_PATH.write_text(json.dumps(token, indent=2))


def _post_to_linkedin_impl(content: str, image_path: str = "") -> dict[str, Any]:
    """Synchronous implementation of LinkedIn post logic."""
    token = _load_token()
    if not token:
        return {"error": "LinkedIn not authenticated. Visit /linkedin/auth to authorize."}

    # Check token expiry
    if token.get("expires_at", 0) <= time.time() + 60:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            return {"error": "LinkedIn access token expired and no refresh token available."}
        # Attempt refresh synchronously
        try:
            import httpx

            resp = httpx.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": token.get("client_id"),
                    "client_secret": token.get("client_secret"),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if resp.status_code != 200:
                return {"error": f"LinkedIn token refresh failed ({resp.status_code})"}
            data = resp.json()
            now = int(time.time())
            token["access_token"] = data["access_token"]
            token["expires_at"] = now + data.get("expires_in", 5184000)
            if "refresh_token" in data:
                token["refresh_token"] = data["refresh_token"]
            _save_token(token)
        except Exception as e:
            return {"error": f"LinkedIn token refresh failed: {e}"}

    user_id = token.get("user_id")
    if not user_id:
        return {"error": "LinkedIn user ID missing. Re-authenticate."}

    auth_headers = {
        "Authorization": f"Bearer {token['access_token']}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    try:
        import httpx

        share_content: dict[str, Any] = {
            "shareCommentary": {"text": content},
            "shareMediaCategory": "NONE",
        }

        post_body = {
            "author": f"urn:li:person:{user_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

        resp = httpx.post(_UGC_POSTS_URL, json=post_body, headers=auth_headers, timeout=30)
        if resp.status_code not in (200, 201):
            return {"error": f"LinkedIn post failed ({resp.status_code}): {resp.text}"}

        post_id = resp.headers.get("x-restli-id", "unknown")
        return {"ok": True, "post_id": post_id, "message": "Posted successfully."}
    except Exception as e:
        return {"error": f"LinkedIn post failed: {e}"}


@router.post("/post")
async def post_to_linkedin(
    body: LinkedInPostRequest,
    request: Request,
    caller_mind: str = Depends(require_api_token),
) -> Any:
    """Post to LinkedIn.

    Precondition (token available) is checked before HITL so the owner is
    never asked to approve an operation that will immediately fail.
    """
    token = _load_token()
    if token is None:
        return {"error": "LinkedIn token file not found. Authenticate first."}
    # Token exists — now request HITL approval if configured. The call is
    # captured so an approval can replay it, and the replay itself (the
    # internal caller) is the approved call rather than a new request.
    if caller_mind != "internal":
        raw = await request.body()
        await _check_hitl(
            "linkedin_post", caller_mind,
            tool_method=request.method, tool_path=str(request.url.path),
            tool_body=raw.decode("utf-8", errors="replace"))
    return _post_to_linkedin_impl(body.content, body.image_path)
