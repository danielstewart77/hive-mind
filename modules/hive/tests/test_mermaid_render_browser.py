"""Exercise the actual description renderer in a browser."""
from pathlib import Path

from playwright.sync_api import sync_playwright


def test_description_renderer_draws_valid_diagram_and_reports_invalid_source():
    script = Path(__file__).resolve().parents[1] / 'src/static/markdown.js'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page()
        page.set_content('<div id="description"></div>')
        page.add_script_tag(path=str(script))
        page.evaluate('''async () => {
            await window.hiveMarkdown.render(document.querySelector('#description'),
                'Before\\n\\n```mermaid\\nflowchart TD\\n A[Start] --> B[End]\\n```\\n\\n'
                + '```mermaid\\ninvalid ! !\\n```\\n\\nAfter');
        }''')
        body = page.locator('#description')
        assert body.locator('.md-diagram svg').count() == 1
        assert body.locator('.md-diagram svg').is_visible()
        assert 'Start' in body.locator('.md-diagram').inner_text()
        assert 'End' in body.locator('.md-diagram').inner_text()
        assert 'could not be drawn' in body.locator('.md-diagram-notice').inner_text()
        assert 'invalid ! !' in body.locator('pre code').inner_text()
        assert 'Before' in body.inner_text() and 'After' in body.inner_text()
        browser.close()
