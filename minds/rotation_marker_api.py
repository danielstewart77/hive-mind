"""Dropping a replaced conversation's rotation marker, on the gateway's word.

A rotation is staged on disk under the mind's ``data/auto-remember`` — the
marker the pane's fire hook reads, the background-task snapshot beside it, and
the composing lock — keyed by the conversation id. When the gateway hands a
conversation over to a fresh one, the old id's marker would otherwise outlive
it, and only the mind's own filesystem holds it. So the gateway asks the mind
to drop it, the same shape as every other per-mind state the mind reports on.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

#: A conversation id is a file-name segment here, and arrives over HTTP.
_SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

#: Every file the rotation hooks keep for one conversation.
_MARKER_FILES = ("rotation-staged-{sid}.json", "bgtasks-{sid}.json", "rotation-{sid}.lock")


def state_dir(mind_dir: Path) -> Path:
    """Where this mind's rotation hooks keep their state: the directory the
    hooks themselves resolve, ``AUTO_REMEMBER_LOG_DIR`` first."""
    explicit = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    return Path(explicit) if explicit else mind_dir / "data" / "auto-remember"


def drop_markers(mind_dir: Path, claude_sid: str) -> None:
    """Delete one conversation's marker files; absent ones are already gone."""
    root = state_dir(mind_dir)
    for pattern in _MARKER_FILES:
        try:
            (root / pattern.format(sid=claude_sid)).unlink()
        except FileNotFoundError:
            pass


def install_rotation_marker_route(app: FastAPI, *, mind_dir: Path) -> None:
    """``DELETE /sessions/{id}/rotation-marker?claude_sid=`` — 204 when gone.

    Under ``/sessions``, so the session guard every such route answers to
    covers it too.
    """

    @app.delete("/sessions/{sid}/rotation-marker")
    async def delete_rotation_marker(sid: str, claude_sid: str = "") -> Response:
        del sid
        if not _SID_RE.fullmatch(claude_sid):
            return JSONResponse({"error": "claude_sid required"}, status_code=400)
        try:
            drop_markers(mind_dir, claude_sid)
        except OSError as exc:
            return JSONResponse({"error": f"could not drop the marker: {exc}"},
                                status_code=500)
        return Response(status_code=204)
