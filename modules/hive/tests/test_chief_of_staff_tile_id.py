"""What a Chief of Staff tile shows, read from the page in a real browser.

Daniel, 2026-11-21: his tiles showed no ID. The ID was there, as the first
line of a label drawn at one size and one weight with the name, and could
not be picked out. It is drawn apart from the name now, and what is asserted
here is what he looks for: the item's whole-number ID, on the tile, on
screen.
"""

import time

import kanban_data
from browser_page import ORIGIN, browser, signed_in_client
from hive_data import staff
from hive_site.testing import run

# Every ID the page has drawn over the tree, and where it and the tree are.
_IDS = """() => {
  const graph = document.getElementById('cos-cy').getBoundingClientRect();
  return Array.from(document.querySelectorAll('.cos-id')).map(el => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {text: el.textContent,
            shown: style.display !== 'none' && box.width > 0 && box.height > 0,
            inside: box.top >= graph.top && box.bottom <= graph.bottom &&
                    box.left >= graph.left && box.right <= graph.right};
  });
}"""


async def _work_on_a_board_item():
    now = float(int(time.time()))
    await staff.add_status("in progress")
    item = await kanban_data.create_task("Tidy the tiles")
    task = await staff.create_task("Ledger name for it", "in progress",
                                   now=now)
    await staff.update_task(task["id"], now=now, board_item=item["id"])
    # Work on no board item: it has no ID to show.
    await staff.create_task("Fix the printer", "in progress", now=now)
    return item["item_number"]


def test_a_tile_on_a_board_item_shows_that_items_whole_number_id(monkeypatch):
    number = run(_work_on_a_board_item())
    client = signed_in_client(monkeypatch)
    with browser(client) as context:
        page = context(1400, 900).new_page()
        page.goto(ORIGIN + "/chief-of-staff")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".cos-id")

        drawn = page.evaluate(_IDS)

    assert drawn == [{"text": str(number), "shown": True, "inside": True}]
