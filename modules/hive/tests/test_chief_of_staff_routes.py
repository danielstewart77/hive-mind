"""The Chief of Staff page and its one API call.

The tree itself is covered in `test_chief_of_staff`; what is here is who may
see it, that the page is the page, and that the whole picture arrives in one
answer rather than three that could disagree about the same moment.
"""

import time

import pytest
from starlette.testclient import TestClient

import accounts
import auth
import kanban_data
import main as main_mod
from hive_data import core, staff
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


def test_chief_of_staff_is_the_first_entry_in_the_sidebar(signed_in):
    body = signed_in.get("/overview").text

    nav = body[body.index('class="console-nav"'):]
    first = nav[nav.index('<a href="'):].split('"')[1]

    assert first == "/chief-of-staff"


def test_chief_of_staff_is_the_first_choice_in_the_features_list(signed_in):
    body = signed_in.get("/settings/features").text

    form = body[body.index('id="feature-form"'):]
    first = form[form.index('name="'):].split('"')[1]

    assert first == "chief-of-staff"


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


def test_the_tree_is_built_with_each_minds_turn_as_the_gateway_reads_it(
        signed_in, monkeypatch):
    """The one thing on the page the database cannot say. Without it a mind
    in a long tool call goes dark and a hung one never warns."""
    async def rows():
        # The route reads the real clock, so the conversation is live now.
        at = time.time()
        await staff.add_status("in progress")
        await core.upsert_mind(mind_id="mind-live", name="Cypher",
                               gateway_url="http://localhost:8426", model="m",
                               harness="dsh", registered_at=at, last_seen=at)
        await core.create_session(id="sess-live", surface="telegram",
                                  surface_id="chat-1", mind_id="mind-live",
                                  created_at=at, last_active=at)
        task = await staff.create_task("Live work", "in progress", now=at)
        await staff.attach_session(task["id"], "sess-live", now=at)
        await staff.note_running(task["id"])
    suite.run(rows())

    async def gateway(path, **_):
        assert path == "/sessions/turns"
        return {"turns": [{
            "session_id": "sess-live", "open": True, "reading": "working",
            "tool": "Bash: run the suite", "tool_started_at": NOW - 600,
            "heard_at": NOW - 600, "quiet_at": None, "stalled_at": None,
            "now": NOW}]}
    monkeypatch.setattr(main_mod, "_gateway_json", gateway)

    data = signed_in.get("/api/chief-of-staff").json()

    card = next(n for n in data["nodes"] if n["kind"] == "task")
    assert (card["doing"], card["doing_for"]) == ("Bash: run the suite", 600)
