"""Owner-authorized rename runs the real identity transaction and recovery."""
import uuid

import httpx
import pytest
import yaml
from hive_data import core, broker_ops
from hive_site import testing
import configuration_data
import credentials_data
import holder_credentials


@pytest.fixture
def rename_setup(tmp_path, monkeypatch, builtin_console):
    admin = builtin_console.admin()
    ident = str(uuid.uuid4())
    import multiuser_support
    listing = {'id': ident, 'name': 'original', 'owner': 'daniel'}
    monkeypatch.setattr(multiuser_support, 'MINDS', [listing])
    testing.run(core.upsert_mind(mind_id=ident, name='original', gateway_url='http://original:8420',
                                model='kept-model', harness='dsh', registered_at=1, last_seen=1, owner='daniel'))
    minds = tmp_path / 'minds'
    (minds / 'original' / 'container').mkdir(parents=True)
    (minds / 'original' / 'runtime.yaml').write_text(yaml.safe_dump({'name': 'original', 'mind_id': ident, 'harness': 'dsh'}))
    monkeypatch.setattr(configuration_data, 'HIVE_MIND_MINDS_CONFIG_DIR', minds)
    monkeypatch.setenv('HIVE_MIND_PROJECT_PATH', str(tmp_path))
    monkeypatch.setenv('COMMS_ADMIN_BEARER_TOKEN', 'test-admin')
    monkeypatch.setenv('HIVE_TOOLS_TOKEN', 'test-tools-token')
    env = tmp_path / '.env'
    env.write_text('COMMS_TOKEN_ORIGINAL=kept-secret\nCOMMS_TOKEN_RENAMED=kept-secret\n')
    monkeypatch.setattr(holder_credentials, 'ENV_FILES', {'hive_mind': env})
    monkeypatch.setattr(credentials_data, 'ENV_FILES', {'hive_mind': env})
    state = {'stage': 'reserved', 'reject_runtime': False, 'gate_status': 'prepared'}
    def respond(request):
        path = request.url.path
        if path.endswith('/capabilities'):
            return httpx.Response(200, json={'version': 1})
        if path.endswith('/prepare'):
            return httpx.Response(200, json={'status': state['gate_status']})
        if path.endswith('/release'):
            return httpx.Response(200, json={'status': 'released'})
        if path.endswith('/reserve'):
            return httpx.Response(200, json={'stage': 'reserved'})
        if path.endswith('/authorize'):
            state['stage'] = 'authorized'
        elif path.endswith('/continue'):
            state['stage'] = 'running'
        elif path.endswith('/confirm'):
            state['stage'] = 'committed'
        elif path.endswith('/rollback'):
            state['stage'] = 'rolled_back'
        if path == '/runtime':
            name = 'original' if request.url.host == 'original' else ('wrong' if state['reject_runtime'] else 'renamed')
            return httpx.Response(200, json={'configuration': {'name': name, 'mind_id': ident}})
        return httpx.Response(200, json={'stage': state['stage']})
    state['listing'] = listing
    return admin, ident, tmp_path, httpx.MockTransport(respond), state, builtin_console


def test_owner_rename_preserves_the_registered_mind(rename_setup, monkeypatch):
    import mind_rename
    admin, ident, root, transport, state, console = rename_setup
    monkeypatch.setattr(mind_rename, 'RENAME_DIR', root / 'rename-operations')
    monkeypatch.setattr(mind_rename, 'TRANSPORT', transport)
    monkeypatch.setattr(mind_rename, 'schedule', lambda *args: None)  # scheduler boundary
    # A different person cannot start this rename.
    other = console.settled_user(admin, 'other-user', 'test-password-123')
    refused = other.post(f'/api/minds/{ident}/rename', json={'name': 'renamed'})
    assert refused.status_code == 404
    owner = console.settled_user(admin, 'mind-owner', 'test-password-123')
    testing.run(broker_ops.update_mind(ident, owner='mind-owner'))
    state['listing']['owner'] = 'mind-owner'
    response = owner.post(f'/api/minds/{ident}/rename', json={'name': 'renamed'})
    assert response.status_code == 202, response.text
    operation = testing.run(mind_rename.advance(response.json()['operation_id']))
    assert operation['status'] == 'success'
    row = testing.run(core.get_mind(ident))
    assert (row['name'], row['model'], row['registered_at'], row['owner']) == ('renamed', 'kept-model', 1, 'mind-owner')
    assert testing.run(core.get_mind_by_name('original')) is None
    assert 'kept-secret' not in response.text


def test_wrong_runtime_identity_recovers_the_original_mind(rename_setup, monkeypatch):
    import mind_rename
    admin, ident, root, transport, state, _ = rename_setup
    monkeypatch.setattr(mind_rename, 'RENAME_DIR', root / 'rename-operations')
    monkeypatch.setattr(mind_rename, 'TRANSPORT', transport)
    monkeypatch.setattr(mind_rename, 'schedule', lambda *args: None)
    state['reject_runtime'] = True
    response = admin.post(f'/api/minds/{ident}/rename', json={'name': 'renamed'})
    assert response.status_code == 202, response.text
    operation = testing.run(mind_rename.advance(response.json()['operation_id']))
    assert operation['status'] == 'failed'
    assert testing.run(core.get_mind(ident))['name'] == 'original'
    assert testing.run(core.get_mind_by_name('renamed')) is None


def test_waiting_rename_can_be_cancelled_without_changing_the_mind(rename_setup, monkeypatch):
    import mind_rename
    admin, ident, root, transport, state, _ = rename_setup
    monkeypatch.setattr(mind_rename, 'RENAME_DIR', root / 'rename-operations')
    monkeypatch.setattr(mind_rename, 'TRANSPORT', transport)
    monkeypatch.setattr(mind_rename, 'schedule', lambda *args: None)
    state['gate_status'] = 'waiting'
    response = admin.post(f'/api/minds/{ident}/rename', json={'name': 'renamed'})
    assert response.status_code == 202, response.text
    operation = testing.run(mind_rename.advance(response.json()['operation_id']))
    assert operation['status'] == 'waiting_idle'
    assert admin.patch('/api/minds/original/settings', json={}).status_code == 409
    cancelled = admin.post(f'/api/minds/{ident}/rename/cancel')
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['status'] == 'cancelled'
    assert testing.run(core.get_mind(ident))['name'] == 'original'
    assert not mind_rename.reserved('renamed')


def test_invalid_or_occupied_name_creates_no_rename(rename_setup, monkeypatch):
    import mind_rename
    admin, ident, root, transport, _, _ = rename_setup
    monkeypatch.setattr(mind_rename, 'RENAME_DIR', root / 'rename-operations')
    monkeypatch.setattr(mind_rename, 'TRANSPORT', transport)
    for name in ('../escape', 'original'):
        refused = admin.post(f'/api/minds/{ident}/rename', json={'name': name})
        assert refused.status_code == 409
        assert mind_rename.latest(ident) is None
        assert testing.run(core.get_mind(ident))['name'] == 'original'
