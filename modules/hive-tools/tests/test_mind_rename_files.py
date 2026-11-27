"""P1/P2: physical rename and recovery preserve opaque Mind state."""

import pytest
import json
import subprocess
import yaml


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'hive'
    folder = root / 'minds' / 'original'
    (folder / 'container').mkdir(parents=True)
    (folder / 'data').mkdir()
    (folder / 'data' / 'conversation.json').write_bytes(b'original is user-authored content\x00')
    runtime = {'name': 'original', 'mind_id': 'uuid-kept', 'gateway_url': 'http://original:8420',
               'default_model': 'model-kept', 'rotation_threshold_percent': 82, 'runtime_config_dir': '/usr/src/app/minds/original/.dsh'}
    (folder / 'runtime.yaml').write_text(yaml.safe_dump(runtime))
    compose = {'services': {'original': {'image': 'hive/mind:original', 'container_name': 'hive-mind-original',
        'environment': ['MIND_NAME=original', 'DSH_HOME=/usr/src/app/minds/original/.dsh', 'MIND_PROXY_KEY=${PROXY_TOKEN_ORIGINAL}'],
        'volumes': ['${HOST_PROJECT_DIR}/minds/original:/usr/src/app/minds/original'], 'networks': ['hivemind']}}}
    (folder / 'container' / 'compose.yaml').write_text(yaml.safe_dump(compose))
    (root / 'docker-compose.yml').write_text('include:\n  - minds/original/container/compose.yaml\nservices: {}\nnetworks:\n  hivemind:\n    external: true\n')
    (root / '.env').write_text('HOST_PROJECT_DIR=' + str(root) + '\nPROXY_TOKEN_ORIGINAL=unchanged-key\nUNRELATED=keep\n')
    return root


def test_physical_rename_preserves_conversation_settings_and_internal_paths(project):
    from tools import mind_rename
    operation = mind_rename.reserve(project, 'op-a', 'uuid-kept', 'original', 'renamed', 'hive-site')
    mind_rename.rename_files(project, operation)
    folder = project / 'minds' / 'renamed'
    assert not (project / 'minds' / 'original').exists()
    assert (folder / 'data' / 'conversation.json').read_bytes() == b'original is user-authored content\x00'
    runtime = yaml.safe_load((folder / 'runtime.yaml').read_text())
    assert runtime == {'name': 'renamed', 'mind_id': 'uuid-kept', 'gateway_url': 'http://renamed:8420',
        'default_model': 'model-kept', 'rotation_threshold_percent': 82, 'runtime_config_dir': '/usr/src/app/minds/original/.dsh'}
    compose = yaml.safe_load((folder / 'container' / 'compose.yaml').read_text())
    service = compose['services']['renamed']
    assert service['container_name'] == 'hive-mind-renamed'
    assert '${HOST_PROJECT_DIR}/minds/renamed:/usr/src/app/minds/original' in service['volumes']
    assert '${HOST_PROJECT_DIR}/minds/renamed:/usr/src/app/minds/renamed' in service['volumes']
    assert 'DSH_HOME=/usr/src/app/minds/original/.dsh' in service['environment']
    assert 'MIND_PROXY_KEY=${PROXY_TOKEN_RENAMED}' in service['environment']
    assert 'PROXY_TOKEN_RENAMED=unchanged-key' in (project / '.env').read_text()
    # The suite blocks engine mutations. This CLI call only parses this
    # disposable configuration, and the test container has no Docker socket.
    process = subprocess.Popen(['docker', 'compose', '-f', str(project / 'docker-compose.yml'), 'config', '--format', 'json'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output, errors = process.communicate(timeout=20)
    assert process.returncode == 0, errors
    consumed = json.loads(output)['services']['renamed']
    assert consumed['environment']['MIND_PROXY_KEY'] == 'unchanged-key'
    assert {volume['target'] for volume in consumed['volumes']} == {'/usr/src/app/minds/original', '/usr/src/app/minds/renamed'}
    # Another service's edit while the operation is pending must survive undo.
    with (project / '.env').open('a') as stream:
        stream.write('ADDED_LATER=keep-too\n')
    mind_rename.rollback_files(project, operation)
    assert (project / 'minds' / 'original' / 'data' / 'conversation.json').read_bytes() == b'original is user-authored content\x00'
    assert not folder.exists()
    assert 'PROXY_TOKEN_ORIGINAL=unchanged-key' in (project / '.env').read_text()
    assert 'ADDED_LATER=keep-too' in (project / '.env').read_text()


def test_destination_collision_changes_nothing(project):
    from tools import mind_rename
    (project / 'minds' / 'renamed').mkdir()
    before = (project / '.env').read_bytes()
    with pytest.raises(ValueError, match='exists'):
        mind_rename.reserve(project, 'op-a', 'uuid-kept', 'original', 'renamed', 'hive-site')
    assert (project / '.env').read_bytes() == before
    assert yaml.safe_load((project / 'minds' / 'original' / 'runtime.yaml').read_text())['name'] == 'original'


def test_failed_container_start_restores_files_and_restarts_exact_original(project, monkeypatch):
    from tools import mind_rename
    operation = mind_rename.reserve(project, 'op-a', 'uuid-kept', 'original', 'renamed', 'hive-site')
    operation['stage'] = 'authorized'
    mind_rename.save(project, operation)
    engine = {'original_running': True}
    before_runtime = (project / 'minds/original/runtime.yaml').read_bytes()
    def docker_boundary(command, **kwargs):
        rc, stdout = 0, ''
        if command[1] == 'ps':
            stdout = 'hive-mind-original\n'
        elif command[1:3] == ['inspect', 'hive-mind-original']:
            stdout = json.dumps([{'Config': {'Labels': {'com.docker.compose.service': 'original'}}, 'Image': 'sha256:exact-original', 'State': {'Running': engine['original_running']}}])
        elif command[1] == 'stop':
            engine['original_running'] = False
        elif 'up' in command:
            rc = 1  # external Docker cannot start the new container
        elif command[1:3] == ['inspect', 'hive-mind-renamed']:
            rc = 1  # no new container exists
        elif command[1] == 'start':
            engine['original_running'] = True
        return subprocess.CompletedProcess(command, rc, stdout, '')
    monkeypatch.setattr(subprocess, 'run', docker_boundary)
    with pytest.raises(ValueError, match='container operation failed'):
        mind_rename.transition(project, operation)
    assert engine['original_running']
    assert (project / 'minds/original/runtime.yaml').read_bytes() == before_runtime
    assert not (project / 'minds/renamed').exists()
    assert mind_rename.load(project, 'op-a')['stage'] == 'rolled_back'


def test_rename_is_held_for_approval_then_runs_as_the_same_call_and_requires_idle_gate(project, monkeypatch, db_conn):
    """P2: the held call cannot bypass approval or the safe-turn boundary.
    The authorize call stays open until a person approves it; the same call
    then runs and moves the rename to `authorized`, and the idle gate still
    refuses a continue."""
    import asyncio
    import time
    from tests.scratch import generate_person_key, generate_token, run
    from tools import mind_rename

    monkeypatch.setenv('HIVE_RENAME_PROJECT_DIR', str(project))
    monkeypatch.setenv('HITL_WAIT_SECONDS', '120')

    async def scenario():
        from tests.held import an_app, bearer, the_waiting_request
        _, client = await an_app()
        caller = bearer(await generate_token('ada'))
        approved_by = bearer(await generate_person_key('daniel', admin=True))
        params = {'path': str(project)}
        reserved = await client.post('/docker/mind-renames/reserve', params=params,
            headers=caller, json={'operation_id': 'op-approved', 'mind_id': 'uuid-kept',
                                 'old': 'original', 'new': 'renamed'})
        assert reserved.status_code == 200
        held = asyncio.create_task(client.post(
            '/docker/mind-renames/op-approved/authorize', params=params, headers=caller))
        request_id = await the_waiting_request()
        await asyncio.sleep(0.05)
        assert not held.done()
        assert mind_rename.load(project, 'op-approved')['stage'] == 'reserved'
        decided = await client.post(f'/hitl/{request_id}/respond',
                                    json={'action': 'approve'}, headers=approved_by)
        assert decided.status_code == 200
        released = await asyncio.wait_for(held, 5)
        assert released.status_code == 200
        assert released.json()['stage'] == 'authorized'
        assert mind_rename.load(project, 'op-approved')['stage'] == 'authorized'
        blocked = await client.post('/docker/mind-renames/op-approved/continue',
                                    params=params, headers=caller)
        assert blocked.status_code == 409
        assert mind_rename.load(project, 'op-approved')['stage'] == 'authorized'

    run(scenario())
    assert (project / 'minds/original/data/conversation.json').read_bytes() == b'original is user-authored content\x00'
    assert not (project / 'minds/renamed').exists()


def test_a_renamed_mind_takes_its_key_folder_with_it_and_mounts_it_by_its_new_name(project, tmp_path):
    """Requirement 21: a mind's key is a file in a folder named for it. The
    rename moves that folder and its compose line, so the console (which looks
    the key up by name) and the container (which mounts it) both still find it,
    and no other mind's folder is touched."""
    from tools import mind_rename
    keys = tmp_path / 'keys'
    (keys / 'original').mkdir(parents=True)
    (keys / 'original' / 'key').write_text('the-minds-key-0123456789\n')
    (keys / 'other').mkdir()
    (keys / 'other' / 'key').write_text('another-key-0123456789\n')
    with (project / '.env').open('a') as stream:
        stream.write(f'HOST_KEY_DIR={keys}\n')
    compose_path = project / 'minds' / 'original' / 'container' / 'compose.yaml'
    document = yaml.safe_load(compose_path.read_text())
    document['services']['original']['volumes'].append('${HOST_KEY_DIR}/original:/run/mind-key:ro')
    compose_path.write_text(yaml.safe_dump(document))

    operation = mind_rename.reserve(project, 'op-k', 'uuid-kept', 'original', 'renamed', 'hive-site')
    mind_rename.rename_files(project, operation)

    assert (keys / 'renamed' / 'key').read_text() == 'the-minds-key-0123456789\n'
    assert not (keys / 'original').exists()
    assert (keys / 'other' / 'key').read_text() == 'another-key-0123456789\n'
    service = yaml.safe_load((project / 'minds' / 'renamed' / 'container' / 'compose.yaml'
                              ).read_text())['services']['renamed']
    assert '${HOST_KEY_DIR}/renamed:/run/mind-key:ro' in service['volumes']

    mind_rename.rollback_files(project, operation)

    assert (keys / 'original' / 'key').read_text() == 'the-minds-key-0123456789\n'
    assert not (keys / 'renamed').exists()
