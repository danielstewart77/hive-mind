"""The order number follows dragging and is separate from the ID (story 3.7.5).

The order number is where an item sits: dragged to the top it is 1, its
children 1.1, theirs 1.1.1, and no id changes. The backlog page is driven in
a real Chromium served by the app itself, because the tree a person drags in
is drawn by the page, and a page that numbered on its own showed numbers the
item page and the board cards did not.

Driven through `browser_page`, which needs playwright and its Chromium.
"""

from contextlib import contextmanager

import kanban_data
from browser_page import ORIGIN, browser, signed_in_client
from hive_site.testing import run


def _client(monkeypatch):
    return signed_in_client(monkeypatch, "order")


@contextmanager
def _page(client, path):
    """The page the app renders, loaded and drawn."""
    with browser(client) as context:
        page = context(1280, 900).new_page()
        page.goto(ORIGIN + path)
        page.wait_for_function(
            "document.getElementById('kanban-feedback').textContent === ''")
        yield page


# Every row of the backlog tree, top to bottom: its id, and its Order cell and
# Name read together as one label.
_TREE = """() => Array.from(document.querySelectorAll('#backlog-mount .kanban-item[data-id]'))
  .map(row => [row.dataset.id,
    `${row.querySelector('.kanban-col-order').textContent} ${row.querySelector('.kanban-item-title').textContent}`])"""

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


def test_an_item_dropped_before_a_row_drawn_at_the_top_level_lands_at_the_top_level_before_it(monkeypatch):
    # A story under a story is drawn and numbered at the top level whatever
    # parent it stores. Given that stored parent, the dropped feature went
    # under a story and out of the place it was dropped.
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    feature = run(kanban_data.create_feature(epic["id"], "Feature"))
    host = run(kanban_data.create_story("Host", epic_id=epic["id"]))
    guest = run(kanban_data.create_story("Guest", epic_id=epic["id"]))
    run(kanban_data.set_parent(guest["id"], host["id"]))

    response = client.patch(f"/api/kanban/features/{feature['id']}",
                            json={"before_id": guest["id"]})

    assert response.status_code == 200
    numbers = run(kanban_data.backlog_numbers())
    assert (numbers[epic["id"]], numbers[host["id"]], numbers[feature["id"]],
            numbers[guest["id"]]) == ("1", "1.1", "2", "3")


def test_an_epic_dropped_before_a_feature_is_refused_and_nothing_moves(monkeypatch):
    # An epic numbers at the top level whatever its parent: stored under the
    # feature's epic it would be drawn elsewhere, and deleting that epic
    # would take it along unseen.
    client = _client(monkeypatch)
    host = run(kanban_data.create_epic("Host"))
    feature = run(kanban_data.create_feature(host["id"], "Feature"))
    dropped = run(kanban_data.create_epic("Dropped"))
    before = run(kanban_data.backlog_numbers())

    response = client.patch(f"/api/kanban/epics/{dropped['id']}",
                            json={"before_id": feature["id"]})

    assert response.status_code == 400
    assert run(kanban_data.backlog_numbers()) == before
    assert run(kanban_data.item_relations(dropped["id"]))["parent"] is None


def test_a_type_change_naming_an_items_own_child_as_parent_is_refused_and_nothing_moves(monkeypatch):
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    outer = run(kanban_data.create_feature(epic["id"], "Outer"))
    inner = run(kanban_data.create_feature(epic["id"], "Inner"))
    run(kanban_data.set_parent(inner["id"], outer["id"]))
    before = run(kanban_data.backlog_numbers())

    response = client.patch(f"/api/kanban/features/{outer['id']}",
                            json={"type": "story", "parent_id": inner["id"]})

    assert response.status_code == 400
    assert run(kanban_data.backlog_numbers()) == before


def test_a_retype_that_sends_children_to_the_top_level_puts_them_last_and_no_epic_moves():
    # A feature retyped to a story makes its stories stories-under-a-story,
    # which number at the top level. Holding the positions of the list they
    # left, they landed among the epics and an epic nobody touched moved.
    second = run(kanban_data.create_epic("Second"))
    first = run(kanban_data.create_epic("First"))
    run(kanban_data.place_before(second["id"], first["id"]))
    feature = run(kanban_data.create_feature(first["id"], "Feature"))
    earlier = run(kanban_data.create_story("Earlier", feature_id=feature["id"]))
    later = run(kanban_data.create_story("Later", feature_id=feature["id"]))
    run(kanban_data.place_before(later["id"], earlier["id"]))
    before = run(kanban_data.backlog_numbers())
    assert (before[second["id"]], before[first["id"]]) == ("1", "2")

    run(kanban_data.change_type(feature["id"], "story"))

    numbers = run(kanban_data.backlog_numbers())
    assert (numbers[second["id"]], numbers[first["id"]], numbers[feature["id"]],
            numbers[later["id"]], numbers[earlier["id"]]) == ("1", "2", "2.1", "3", "4")


def test_a_feature_nested_onto_a_story_is_drawn_under_it_with_its_number(monkeypatch):
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    host = run(kanban_data.create_story("Host", epic_id=epic["id"]))
    feature = run(kanban_data.create_feature(epic["id"], "Feature"))
    below = run(kanban_data.create_story("Below", feature_id=feature["id"]))
    run(kanban_data.set_parent(feature["id"], host["id"]))

    with _page(client, "/kanban/backlog") as page:
        tree = page.evaluate(_TREE)

    assert tree == [
        [epic["id"], "1 Epic"],
        [host["id"], "1.1 Host"],
        [feature["id"], "1.1.1 Feature"],
        [below["id"], "1.1.1.1 Below"],
    ]


def test_reordering_stories_drawn_at_the_top_level_keeps_the_story_they_hang_off(monkeypatch):
    client = _client(monkeypatch)
    epic = run(kanban_data.create_epic("Epic"))
    host = run(kanban_data.create_story("Host", epic_id=epic["id"]))
    first = run(kanban_data.create_story("First", epic_id=epic["id"]))
    second = run(kanban_data.create_story("Second", epic_id=epic["id"]))
    run(kanban_data.set_parent(first["id"], host["id"]))
    run(kanban_data.set_parent(second["id"], host["id"]))

    response = client.patch(f"/api/kanban/stories/{second['id']}",
                            json={"before_id": first["id"]})

    assert response.status_code == 200
    numbers = run(kanban_data.backlog_numbers())
    assert (numbers[second["id"]], numbers[first["id"]]) == ("2", "3")
    assert run(kanban_data.item_relations(second["id"]))["parent"]["id"] == host["id"]


def test_a_type_change_that_keeps_an_item_in_its_list_keeps_its_place():
    epic = run(kanban_data.create_epic("Epic"))
    feature = run(kanban_data.create_feature(epic["id"], "Feature"))
    stories = [run(kanban_data.create_story(t, feature_id=feature["id"]))
               for t in ("x", "y", "z")]

    run(kanban_data.change_type(stories[1]["id"], "bug"))

    numbers = run(kanban_data.backlog_numbers())
    assert [numbers[s["id"]] for s in stories] == ["1.1.1", "1.1.2", "1.1.3"]
