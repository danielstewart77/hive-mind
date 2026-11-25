"""Description editing preserves diagram source through the real item save."""
import kanban_data as board
from browser_page import ORIGIN, browser, signed_in_client
from hive_site.testing import run


def test_editing_and_saving_a_description_preserves_mermaid_source(monkeypatch):
    epic = run(board.create_epic('Scratch epic'))
    story = run(board.create_story('Scratch story', epic_id=epic['id']))
    initial = 'Original\n\n```mermaid\nflowchart TD\n A --> B\n```\n'
    run(board.update_story(story['id'], description=initial))
    source = '\nBefore diagram\n\n```mermaid\nflowchart TD\n  A[Start] --> B[End]\n```\n\nAfter diagram\n'
    client = signed_in_client(monkeypatch)
    with browser(client) as context:
        page = context(1366, 900).new_page()
        page.goto(f'{ORIGIN}/kanban/item/{story["id"]}')
        page.locator('#description-toggle').click()
        assert page.locator('#description-input').is_visible()
        assert page.locator('#description-input').input_value() == initial
        page.locator('#description-input').fill(source)
        page.get_by_role('button', name='Save', exact=True).click()
        page.get_by_text('Saved.', exact=True).wait_for()
        page.reload()
        page.locator('#description-toggle').click()
        assert page.locator('#description-input').input_value() == source
    assert run(board.find_item(story['id']))['description'] == source
