"""The backlog's status filter, driven in a real browser (stories 3.7.1, 3.7.4).

What is asserted is what Daniel sees: which titles the backlog draws, read
from the page the app serves, against a board built through the same routes
the page writes with.
"""

from browser_page import ORIGIN, browser, signed_in_client

PAGE = ORIGIN + "/kanban/backlog"

# Every title the backlog tree draws, and the nesting level of its row.
_DRAWN = """() => Object.fromEntries(
  [...document.querySelectorAll('#backlog-mount .kanban-item[data-level]')].map(row => [
    row.querySelector('.kanban-item-title').textContent.replace(/^[0-9.]+ /, ''),
    Number(row.dataset.level)]))"""


def _make(client, kind, title, status, **parent):
    made = client.post(f"/api/kanban/{kind}", json={"title": title, **parent})
    assert made.status_code == 200, made.text
    record = made.json()
    set_status = client.patch(f"/api/kanban/{kind}/{record['id']}", json={"status": status})
    assert set_status.status_code == 200, set_status.text
    return record


def _board(client):
    """One item of every type in every place the filter has to decide on."""
    open_epic = _make(client, "epics", "Open epic", "committed")
    _make(client, "epics", "Done epic", "done")
    done_feature = _make(client, "features", "Done feature", "done", epic_id=open_epic["id"])
    _make(client, "stories", "Open story under the done feature", "in_progress",
          feature_id=done_feature["id"])
    _make(client, "stories", "Done story", "done", epic_id=open_epic["id"])
    _make(client, "tasks", "Open task", "backlog")
    _make(client, "tasks", "Done task", "done")


DONE = {"Done epic", "Done feature", "Done story", "Done task"}
NOT_DONE = {"Open epic", "Open story under the done feature", "Open task"}


def _client_with_board(monkeypatch):
    client = signed_in_client(monkeypatch)
    _board(client)
    return client


_NUMBERS = """() => Object.fromEntries(
  [...document.querySelectorAll('#backlog-mount .kanban-item[data-level]')]
    .map(row => row.querySelector('.kanban-item-title').textContent.match(/^([0-9.]+) (.*)$/))
    .filter(Boolean).map(m => [m[2], m[1]]))"""


def _numbers(page):
    """The number each row is drawn with."""
    page.wait_for_selector("#backlog-mount .kanban-item")
    return page.evaluate(_NUMBERS)


def _drawn(page):
    page.wait_for_selector("#backlog-mount .kanban-item")
    return page.evaluate(_DRAWN)


def test_done_items_are_hidden_until_the_filter_asks_for_them_and_never_deleted(monkeypatch):
    client = signed_in_client(monkeypatch)
    _board(client)

    with browser(client) as context:
        page = context(1280, 900).new_page()
        page.goto(PAGE)

        fresh = _drawn(page)
        assert set(fresh) == NOT_DONE

        page.check('#status-filter input[value="done"]')
        assert set(_drawn(page)) == DONE

        page.click("#status-filter-clear")
        assert set(_drawn(page)) == NOT_DONE
        assert not page.is_checked('#status-filter input[value="done"]')

        page.check('#status-filter input[value="committed"]')
        assert set(_drawn(page)) == {"Open epic"}

        # Every status shown: the rows the default drew keep their numbers.
        for status in ("backlog", "in_progress", "done"):
            page.check(f'#status-filter input[value="{status}"]')
        assert set(_drawn(page)) == DONE | NOT_DONE
        everything = _numbers(page)
        page.click("#status-filter-clear")
        default = _numbers(page)
        assert set(default) == NOT_DONE
        assert default == {title: everything[title] for title in default}

    # Hidden is not removed: every item is still on the board.
    board = client.get("/api/kanban/data").json()
    titles = {r["title"] for kind in ("epics", "features", "stories", "tasks")
              for r in board[kind]}
    assert titles == DONE | NOT_DONE


def test_an_item_set_to_done_from_its_row_leaves_the_backlog(monkeypatch):
    client = _client_with_board(monkeypatch)

    with browser(client) as context:
        page = context(1280, 900).new_page()
        page.goto(PAGE)
        _drawn(page)
        page.select_option('select[aria-label="Status for Open task"]', "done")
        page.wait_for_function(
            "() => !document.querySelector('select[aria-label=\"Status for Open task\"]')")
        assert set(_drawn(page)) == NOT_DONE - {"Open task"}

    tasks = client.get("/api/kanban/data").json()["tasks"]
    assert {t["title"]: t["status"] for t in tasks}["Open task"] == "done"


def test_a_standalone_task_is_a_top_level_row_of_the_backlog(monkeypatch):
    client = signed_in_client(monkeypatch)
    epic = _make(client, "epics", "An epic", "backlog")
    _make(client, "features", "A feature", "backlog", epic_id=epic["id"])
    task = _make(client, "tasks", "Stand alone", "backlog")
    story = _make(client, "stories", "Under the task", "backlog", epic_id=epic["id"])
    moved = client.patch(f"/api/kanban/stories/{story['id']}", json={"parent_id": task["id"]})
    assert moved.status_code == 200, moved.text

    with browser(client) as context:
        page = context(1280, 900).new_page()
        page.goto(PAGE)
        drawn = _drawn(page)

    assert drawn["Stand alone"] == 1
    assert drawn["A feature"] == 2
    assert drawn["Under the task"] == 2
