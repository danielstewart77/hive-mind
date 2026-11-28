"""Render the real authenticated form using a disposable database only."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from hive_site import testing

import auth
import main
import store
from starlette.testclient import TestClient


async def setup():
    scratch = await testing.ScratchDatabase.create()
    os.environ["HIVE_DATABASE_URL"] = scratch.dsn
    os.environ["HIVE_SECRET_KEY"] = "disposable-browser-fixture"
    await store.open()
    user = await auth.create_user("browser-fixture", "disposable-only-password", is_admin=True)
    return scratch, await auth.create_session_token(user)


with testing.testclients_on_the_suite_loop():
    scratch, cookie = testing.run(setup())
    try:
        client = TestClient(main.app)
        client.cookies.set(auth.SESSION_COOKIE_NAME, cookie)
        response = client.get("/minds/new")
        assert response.status_code == 200, response.status_code
        Path(sys.argv[1]).write_text(response.text)
    finally:
        testing.run(store.close())
        testing.run(scratch.close())
        testing.stop_suite_portal()
