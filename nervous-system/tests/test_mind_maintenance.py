"""A rename cannot take an answering Mind or its attached terminal away."""
import asyncio
import os
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import hive_data
from starlette.requests import Request
from hive_data import core, migrate, testing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.asyncio
async def test_prepare_waits_for_turn_and_terminal_then_blocks_only_target(monkeypatch):
    from comms import mind_maintenance
    admin = os.environ['HIVE_DATA_TEST_ADMIN_DSN']
    async with testing.claimed_database(admin, 'lucent_test_') as dsn:
        await migrate.apply(dsn)
        monkeypatch.setenv(hive_data.config.DSN_VAR, dsn)
        await hive_data.connect()
        try:
            await core.upsert_mind(mind_id='target', name='target', gateway_url='http://target:8420', model='m', harness='dsh', registered_at=1, last_seen=1)
            await core.upsert_mind(mind_id='other', name='other', gateway_url='http://other:8420', model='m', harness='dsh', registered_at=1, last_seen=1)
            await core.create_session(id='session-a', mind_id='target', surface='terminal', surface_id='pane-a', created_at=1, last_active=1)
            await core.update_session('session-a', terminal_held=True)
            locks = {'session-a': asyncio.Lock()}
            await locks['session-a'].acquire()
            assert (await mind_maintenance.prepare('target', 'op-a', locks))['status'] == 'waiting'
            locks['session-a'].release()
            assert (await mind_maintenance.prepare('target', 'op-a', locks))['status'] == 'waiting'
            await core.update_session('session-a', terminal_held=False, surface='web')
            assert (await mind_maintenance.prepare('target', 'op-a', locks))['status'] == 'prepared'
            from comms.sessions import SessionManager
            manager = SessionManager()
            manager._locks = locks
            turn = manager.send_message('session-a', 'do not lose or dispatch this turn')
            with pytest.raises(ValueError, match='renamed'):
                await anext(turn)
            await turn.aclose()
            with pytest.raises(ValueError, match='renamed'):
                await manager._spawn('session-a', 'm', resume_sid='conversation-kept', mind_id='target')
            with pytest.raises(ValueError, match='renamed'):
                await mind_maintenance.check('target')
            await mind_maintenance.check('other')
            with pytest.raises(ValueError):
                await mind_maintenance.release('target', 'someone-else')
            from comms import server as comms_server
            monkeypatch.setattr(comms_server, 'session_mgr', manager)
            async def receive():
                return {'type': 'http.request', 'body': json.dumps({'operation_id': 'op-a'}).encode(), 'more_body': False}
            request = Request({'type': 'http', 'method': 'POST', 'headers': []}, receive)
            assert (await comms_server.prepare_mind_rename('target', request))['status'] == 'prepared'
            manager._procs['session-a'] = {'_mind_url': 'http://target:8420'}
            await core.rename_mind('target', 'target', 'renamed-target')
            assert (await comms_server.release_mind_rename('target', request))['status'] == 'released'
            assert manager._procs['session-a']['_mind_url'] == 'http://renamed-target:8420'
            await mind_maintenance.check('target')
        finally:
            await hive_data.close()


@pytest.mark.asyncio
async def test_gateway_shutdown_preserves_mind_owned_terminal_process(monkeypatch):
    from comms.sessions import SessionManager
    remote = {'alive': True}
    class MindBoundary(BaseHTTPRequestHandler):
        def do_DELETE(self):
            remote['alive'] = False
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), MindBoundary)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with testing.claimed_database(os.environ['HIVE_DATA_TEST_ADMIN_DSN'], 'lucent_test_') as dsn:
            await migrate.apply(dsn)
            monkeypatch.setenv(hive_data.config.DSN_VAR, dsn)
            await hive_data.connect()
            try:
                await core.upsert_mind(mind_id='target', name='target', gateway_url=f'http://127.0.0.1:{server.server_port}', model='m', harness='dsh', registered_at=1, last_seen=1, session_token='fixture-session-key')
                await core.create_session(id='session-a', mind_id='target', conversation_id='kept-conversation', surface='terminal', surface_id='pane-a', created_at=1, last_active=1)
                manager = SessionManager()
                manager._procs['session-a'] = {'_mind_url': f'http://127.0.0.1:{server.server_port}'}
                manager._mind_ids['session-a'] = 'target'
                await manager.shutdown()
                assert remote['alive'], 'Restarting the gateway killed a Mind-owned terminal'
                assert (await core.get_session('session-a'))['conversation_id'] == 'kept-conversation'
            finally:
                await hive_data.close()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()
