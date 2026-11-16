"""Drive a real console page in headless Chromium, served by the app itself.

The page under test is the one the app renders — template, static files and
API — answered in-process by a TestClient on the suite's scratch database.
Chromium never reaches a server of ours over the network; only the page's
third-party scripts (Cytoscape on cdnjs) are fetched for real, because the
behaviour under test depends on how that library sizes its canvas. Fonts and
the icon set are dropped: nothing here depends on them.

Needs `playwright` (requirements-dev.txt) and its Chromium:
`.venv/bin/python -m playwright install chromium`.
"""

from contextlib import contextmanager

from playwright.sync_api import sync_playwright
from starlette.testclient import TestClient

import auth
import main as main_mod
from hive_site.testing import run

ORIGIN = "http://testserver"
_DROPPED = ("fonts.googleapis.com", "fonts.gstatic.com", "unpkg.com")
_HOP_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}


def signed_in_client(monkeypatch, username="browser"):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user(username, "secret-pass", is_admin=True, replace=True))
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return client


def _router(client, closing):
    def handle(route, request):
        url = request.url
        # A page polls on a timer; one that fires while the browser is being
        # closed must not reach a database the test is about to drop.
        if closing or any(host in url for host in _DROPPED):
            return route.abort()
        if not url.startswith(ORIGIN):
            return route.continue_()
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        response = client.request(request.method, url[len(ORIGIN):],
                                  headers=headers, content=request.post_data_buffer)
        route.fulfill(
            status=response.status_code,
            headers={k: v for k, v in response.headers.items()
                     if k.lower() not in _HOP_HEADERS},
            body=response.content,
        )
    return handle


@contextmanager
def browser(client):
    """One Chromium for a test, yielding `context(width, height, touch)`."""
    with sync_playwright() as p:
        chromium = p.chromium.launch()
        closing = []
        try:
            def context(width, height, touch=False):
                ctx = chromium.new_context(viewport={"width": width, "height": height},
                                           has_touch=touch, is_mobile=touch)
                ctx.route("**/*", _router(client, closing))
                return ctx
            yield context
        finally:
            closing.append(True)
            chromium.close()
