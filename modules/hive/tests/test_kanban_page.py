"""The kanban page itself: who may open it, and that it is the page served."""
import pytest
from starlette.testclient import TestClient

import auth
import main as main_mod


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("kanban-page", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_a_signed_out_visitor_is_sent_to_log_in(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    response = TestClient(main_mod.app).get("/kanban", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/kanban")


def test_a_signed_in_visitor_is_served_the_board(signed_in):
    assert signed_in.get("/kanban").status_code == 200
