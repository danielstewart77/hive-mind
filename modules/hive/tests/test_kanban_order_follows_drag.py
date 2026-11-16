"""The order number follows dragging and is separate from the ID (story 3.7.5).

The order number is where an item sits: dragged to the top it is 1, its
children 1.1, theirs 1.1.1, and no id changes. The backlog page is driven in
a real Chromium served by the app itself, because the tree a person drags in
is drawn by the page, and a page that numbered on its own showed numbers the
item page and the board cards did not.

Needs `playwright` (requirements-dev.txt) and its Chromium:
`.venv/bin/python -m playwright install chromium`.
"""

from contextlib import contextmanager

from playwright.sync_api import sync_playwright
from starlette.testclient import TestClient

import accounts
import auth
import kanban_data
import main as main_mod
from hive_site.testing import run

ORIGIN = "http://testserver"
# Fonts and the icon set are fetched from third parties; nothing here needs them.
_DROPPED = ("fonts.googleapis.com", "fonts.gstatic.com", "unpkg.com")
_HOP_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}


def _client(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("order", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


@contextmanager
def _page(client, path):
    """The page the app renders, answered in-process by the TestClient."""
    closing = []

    def handle(route, request):
        url = request.url
        if closing or any(host in url for host in _DROPPED):
            return route.abort()
        if not url.startswith(ORIGIN):
            return route.continue_()
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        response = client.request(request.method, url[len(ORIGIN):],
                                  headers=headers, content=request.post_data_buffer)
        route.fulfill(status=response.status_code,
                      headers={k: v for k, v in response.headers.items()
                               if k.lower() not in _HOP_HEADERS},
                      body=response.content)

    with sync_playwright() as p:
        chromium = p.chromium.launch()
        try:
            context = chromium.new_context(viewport={"width": 1280, "height": 900})
            context.route("**/*", handle)
            page = context.new_page()
            page.goto(ORIGIN + path)
            page.wait_for_function(
                "document.getElementById('kanban-feedback').textContent === ''")
            yield page
        finally:
            closing.append(True)
            chromium.close()


# Every row of the backlog tree, top to bottom: its id and the label it shows.
_TREE = """() => Array.from(document.querySelectorAll('#backlog-mount .kanban-item[data-id]'))
  .map(row => [row.dataset.id, row.querySelector('.kanban-item-title').textContent])"""

# The cards of one board lane, top to bottom.
_LANE = """status => Array.from(
  document.querySelectorAll(`.kanban-drop[data-status="${status}"] .kanban-card`))
  .map(card => card.dataset.id)"""


def _ids():
    data = run(kanban_data.get_data())
    return sorted(r["id"] for kind in data.values() for r in kind)


def test_dragging_an_epic_to_the_top_makes_it_1_its_features_1_1_and_their_stories_1_1_1(monkeypatch):
    client = _client(monkeypatch)
    moved = run(kanban_data.create_epic("Moved"))
    feature = run(kanban_data.create_feature(moved["id"], "Its feature"))
    story = run(kanban_data.create_story("Its story", feature_id=feature["id"]))
    top = run(kanban_data.create_epic("Was on top"))
    ids = _ids()

    with _page(client, "/kanban/backlog") as page:
        source = page.locator(f'#backlog-mount .kanban-item[data-id="{moved["id"]}"]')
        target = page.locator(f'#backlog-mount .kanban-item[data-id="{top["id"]}"]')
        # The top band of a row is the reorder zone: dropped there, the item
        # lands directly above that row.
        source.drag_to(target, source_position={"x": 3, "y": 10}, target_position={"x": 40, "y": 3})
        page.wait_for_function(
            "document.getElementById('kanban-feedback').textContent === 'Moved.'")
        tree = page.evaluate(_TREE)

    assert tree == [
        [moved["id"], "1 Moved"],
        [feature["id"], "1.1 Its feature"],
        [story["id"], "1.1.1 Its story"],
        [top["id"], "2 Was on top"],
    ]
    assert _ids() == ids


def test_the_tree_draws_a_story_dragged_above_a_feature_where_it_now_sits(monkeypatch):
    # An epic's features and its own stories are one list. Drawn kind by
    # kind, the story stayed below both features whatever its number said.
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    first = run(kanban_data.create_feature(epic["id"], "First feature"))
    second = run(kanban_data.create_feature(epic["id"], "Second feature"))
    story = run(kanban_data.create_story("Direct story", epic_id=epic["id"]))
    ids = _ids()

    with _page(client, "/kanban/backlog") as page:
        source = page.locator(f'#backlog-mount .kanban-item[data-id="{story["id"]}"]')
        target = page.locator(f'#backlog-mount .kanban-item[data-id="{first["id"]}"]')
        page.locator(f'.kanban-item[data-id="{epic["id"]}"] .kanban-toggle').first.click()
        source.drag_to(target, source_position={"x": 3, "y": 10}, target_position={"x": 40, "y": 3})
        page.wait_for_function(
            "document.getElementById('kanban-feedback').textContent === 'Moved.'")
        tree = page.evaluate(_TREE)

    assert tree == [
        [epic["id"], "1 Epic"],
        [story["id"], "1.1 Direct story"],
        [first["id"], "1.2 First feature"],
        [second["id"], "1.3 Second feature"],
    ]
    numbers = run(kanban_data.backlog_numbers())
    assert [numbers[i] for i, _ in tree] == ["1", "1.1", "1.2", "1.3"]
    assert _ids() == ids


def test_a_board_lane_lists_its_cards_in_order_number_order(monkeypatch):
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    first = run(kanban_data.create_feature(epic["id"], "First"))
    second = run(kanban_data.create_feature(epic["id"], "Second"))
    under_first = run(kanban_data.create_story("Under first", feature_id=first["id"],
                                               status="committed"))
    under_second = run(kanban_data.create_story("Under second", feature_id=second["id"],
                                                status="committed"))
    run(kanban_data.place_before(second["id"], first["id"]))

    with _page(client, "/kanban/board") as page:
        lane = page.evaluate(_LANE, "committed")

    assert lane == [under_second["id"], under_first["id"]]


def test_reordering_one_kind_keeps_the_place_of_the_other_kind_in_the_same_list():
    # Renumbered one kind at a time, the features took 1..n beside a story
    # already holding 1, and the tie was settled by the random id.
    epic = run(kanban_data.create_epic("Epic"))
    first = run(kanban_data.create_feature(epic["id"], "First"))
    second = run(kanban_data.create_feature(epic["id"], "Second"))
    story = run(kanban_data.create_story("Story", epic_id=epic["id"]))
    run(kanban_data.place_before(story["id"], first["id"]))

    run(kanban_data.update_feature(second["id"], before_id=first["id"]))

    numbers = run(kanban_data.backlog_numbers())
    assert (numbers[story["id"]], numbers[second["id"]], numbers[first["id"]]) == (
        "1.1", "1.2", "1.3")


def test_dropping_an_item_onto_its_own_child_is_refused_and_both_keep_their_place(monkeypatch):
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    parent = run(kanban_data.create_feature(epic["id"], "Parent"))
    child = run(kanban_data.create_feature(epic["id"], "Child"))
    run(kanban_data.set_parent(child["id"], parent["id"]))
    before = run(kanban_data.backlog_numbers())

    response = client.patch(f"/api/kanban/features/{parent['id']}",
                            json={"parent_id": child["id"]})

    assert response.status_code == 400
    assert run(kanban_data.backlog_numbers()) == before
    assert before[parent["id"]] == "1.1" and before[child["id"]] == "1.1.1"


def test_a_reorder_drop_before_an_items_own_child_is_refused_and_nothing_moves(monkeypatch):
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    outer = run(kanban_data.create_feature(epic["id"], "Outer"))
    inner = run(kanban_data.create_feature(epic["id"], "Inner"))
    run(kanban_data.set_parent(inner["id"], outer["id"]))
    deepest = run(kanban_data.create_story("Deepest", feature_id=inner["id"]))
    before = run(kanban_data.backlog_numbers())

    # Landing before `deepest` would give `outer` the parent `inner`, which
    # hangs off `outer`.
    response = client.patch(f"/api/kanban/features/{outer['id']}",
                            json={"before_id": deepest["id"]})

    assert response.status_code == 400
    assert run(kanban_data.backlog_numbers()) == before
