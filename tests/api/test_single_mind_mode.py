"""API tests for single-mind mode in server.py.

When MIND_ID env var is set, POST /sessions should only accept matching
mind_id values and reject all others with 403.
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def _session_response(mind_id: str = "bilby") -> dict:
    return {
        "id": "sess-1",
        "claude_sid": None,
        "owner_type": "test",
        "owner_ref": "user-1",
        "summary": "New session",
        "model": "sonnet",
        "autopilot": False,
        "created_at": 1000.0,
        "last_active": 1000.0,
        "status": "running",
        "epilogue_status": None,
        "mind_id": mind_id,
    }


class TestSingleMindMode:
    """Tests for MIND_ID env var restricting session creation."""

    def test_create_session_single_mind_mode_accepts_matching_mind_id(self):
        """With MIND_ID=bilby, POST /sessions with mind_id=bilby returns 200."""
        with patch.dict(os.environ, {"MIND_ID": "bilby"}), \
             patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value=_session_response("bilby"))

            # Force reload of _SINGLE_MIND
            import server
            original = server._SINGLE_MIND
            server._SINGLE_MIND = "bilby"
            try:
                client = TestClient(server.app, raise_server_exceptions=False)
                response = client.post("/sessions", json={
                    "owner_type": "test",
                    "owner_ref": "user-1",
                    "client_ref": "client-1",
                    "mind_id": "bilby",
                })
                assert response.status_code == 200
            finally:
                server._SINGLE_MIND = original

    def test_create_session_single_mind_mode_rejects_wrong_mind_id(self):
        """With MIND_ID=bilby, POST /sessions with mind_id=ada returns 403."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value=_session_response("ada"))

            import server
            original = server._SINGLE_MIND
            server._SINGLE_MIND = "bilby"
            try:
                client = TestClient(server.app, raise_server_exceptions=False)
                response = client.post("/sessions", json={
                    "owner_type": "test",
                    "owner_ref": "user-1",
                    "client_ref": "client-1",
                    "mind_id": "ada",
                })
                assert response.status_code == 403
                data = response.json()
                assert "error" in data
            finally:
                server._SINGLE_MIND = original

    def test_create_session_single_mind_mode_defaults_to_mind_id(self):
        """With MIND_ID=bilby, POST /sessions without explicit mind_id uses bilby."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value=_session_response("bilby"))

            import server
            original = server._SINGLE_MIND
            server._SINGLE_MIND = "bilby"
            try:
                client = TestClient(server.app, raise_server_exceptions=False)
                response = client.post("/sessions", json={
                    "owner_type": "test",
                    "owner_ref": "user-1",
                    "client_ref": "client-1",
                })
                assert response.status_code == 200
                call_kwargs = mock_mgr.create_session.call_args.kwargs
                assert call_kwargs.get("mind_id") == "bilby"
            finally:
                server._SINGLE_MIND = original

    def test_normal_mode_no_mind_id_restriction(self):
        """Without MIND_ID env, any mind_id is accepted (existing behavior)."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value=_session_response("nagatha"))

            import server
            original = server._SINGLE_MIND
            server._SINGLE_MIND = None
            try:
                client = TestClient(server.app, raise_server_exceptions=False)
                response = client.post("/sessions", json={
                    "owner_type": "test",
                    "owner_ref": "user-1",
                    "client_ref": "client-1",
                    "mind_id": "nagatha",
                })
                assert response.status_code == 200
            finally:
                server._SINGLE_MIND = original
