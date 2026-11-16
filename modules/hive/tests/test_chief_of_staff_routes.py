"""The Chief of Staff page and its one API call.

The tree itself is covered in `test_chief_of_staff`; what is here is who may
see it, that the page is the page, and that the whole picture arrives in one
answer rather than three that could disagree about the same moment.
"""

import pytest
from starlette.testclient import TestClient

import accounts
import auth
import kanban_data
import main as main_mod
from hive_data import staff
from hive_site import testing as suite

NOW = 1_800_000_000.0


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("cos-page", "secret-pass", is_admin=True,
                              replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def test_the_page_sends_a_signed_out_browser_to_the_login_form(monkeypatch):
    """A page, not an API: a bare 401 is not something to read."""
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.get("/chief-of-staff", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/chief-of-staff"


def test_the_tree_is_refused_to_a_browser_that_is_not_signed_in(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/chief-of-staff").status_code == 401


def test_the_page_renders_and_is_reachable_from_the_sidebar(signed_in):
    body = signed_in.get("/chief-of-staff")

    assert body.status_code == 200
    assert 'id="cos-cy"' in body.text
    assert 'href="/chief-of-staff"' in body.text


def test_one_call_answers_with_the_tree_the_barrel_and_the_questions(
        signed_in):
    """Three calls could be three different moments and disagree."""
    async def rows():
        for name in ("in progress", "done"):
            await staff.add_status(name)
        live = await staff.create_task("Live work", "in progress", now=NOW)
        await staff.update_task(live["id"], question="Which model?", now=NOW)
        epic = await kanban_data.create_epic("Chief of Staff")
        feature = await kanban_data.create_feature(epic["id"], "The page")
        story = await kanban_data.create_story("Shipped",
                                               feature_id=feature["id"])
        await kanban_data.update_story(story["id"], status="done")
        shipped = await staff.create_task("Shipped", "done", now=NOW)
        await staff.update_task(shipped["id"], board_item=story["id"], now=NOW)
    suite.run(rows())

    data = signed_in.get("/api/chief-of-staff").json()

    assert [n["label"] for n in data["nodes"] if n["kind"] == "task"] == [
        "Live work"]
    assert [b["label"].split(" ", 1)[1] for b in data["barrel"]] == [
        "Shipped"]
    assert [q["question"] for q in data["questions"]] == ["Which model?"]
    assert next(n for n in data["nodes"]
                if n["kind"] == "chief")["questions"] == 1


def test_the_barrel_window_is_seven_days_unless_the_page_asks_for_another(
        signed_in):
    """Feature 6.11: with no window named the answer reaches back seven
    days; a wider one brings older finished work back."""
    import time
    day = 24 * 60 * 60

    async def rows():
        await staff.add_status("done")
        now = time.time()
        recent = await staff.create_task("Six days ago", "done",
                                         now=now - 6 * day)
        old = await staff.create_task("Just over seven days ago", "done",
                                      now=now - 7 * day - 60)
        return recent["id"], old["id"]
    recent, old = suite.run(rows())

    default = signed_in.get("/api/chief-of-staff").json()
    wide = signed_in.get("/api/chief-of-staff?days=30").json()

    assert [b["id"] for b in default["barrel"]] == [recent]
    assert [b["id"] for b in wide["barrel"]] == [recent, old]
