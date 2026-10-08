import os, sys
from starlette.testclient import TestClient
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import auth, main as main_mod

def client(tmp_path, monkeypatch):
    monkeypatch.setenv('HIVE_DB_PATH', str(tmp_path/'hive.db')); monkeypatch.setenv('HIVE_SECRET_KEY','test-secret')
    user=auth.create_user('kanban-page','secret-pass',is_admin=True,replace=True)
    c=TestClient(main_mod.app); c.cookies.set(auth.SESSION_COOKIE_NAME,auth.create_session_token(user)); return c

def test_kanban_page_auth_and_render(tmp_path,monkeypatch):
    monkeypatch.setenv('HIVE_DB_PATH',str(tmp_path/'hive.db')); monkeypatch.setenv('HIVE_SECRET_KEY','test-secret')
    anon=TestClient(main_mod.app); r=anon.get('/kanban',follow_redirects=False)
    assert r.status_code==303 and r.headers['location'].startswith('/login?next=/kanban')
    r=client(tmp_path,monkeypatch).get('/kanban'); assert r.status_code==200
    assert r.headers['cache-control']=='no-store, must-revalidate' and r.headers['pragma']=='no-cache'
    assert 'board-mount' in r.text and 'backlog-mount' in r.text
    assert r.text.index('square-kanban') < r.text.index('href="/canvas"')
    assert '>Kanban</span>' in r.text
    kanban_link = r.text.split('href="/kanban"', 1)[1].split('>', 1)[0]
    assert 'is-active' in kanban_link
