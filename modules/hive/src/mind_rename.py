"""Owner-requested full rename, with durable host/database reconciliation."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import time
import uuid

import httpx
from hive_data import core
from starlette.responses import JSONResponse

import configuration_data
import holder_credentials
import mind_mounts
import mind_creation
import mind_runtime
from fastapi import HTTPException
import provider_data

RENAME_DIR = Path(os.getenv('HIVE_MIND_RENAMES_DIR', '/data/mind-renames'))
TRANSPORT = None  # HTTP boundary, overridden only by isolated tests
_REQUEST_LOCK = asyncio.Lock()
_TASKS = {}
_STEP_LOCKS = {}
FINAL = {'success', 'failed', 'cancelled'}


class RenameServiceError(ValueError):
    def __init__(self, status):
        super().__init__(f'Rename service refused the operation (HTTP {status})')
        self.status = status


class EditGate:
    """After ownership authorization, reserve a pending Mind against edits."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        parts = scope.get('path', '').strip('/').split('/')
        if (scope.get('type') == 'http' and scope.get('method') in ('POST', 'PUT', 'PATCH', 'DELETE')
                and len(parts) > 3 and parts[:2] == ['api', 'minds'] and parts[3] != 'rename'):
            row = await core.get_mind_by_id_or_name(parts[2])
            operation = latest(row['mind_id']) if row else None
            if operation and operation['status'] not in FINAL:
                return await JSONResponse({'detail': 'Wait for this Mind’s rename to finish before editing it'}, status_code=409)(scope, receive, send)
        await self.app(scope, receive, send)


def _path(operation_id):
    return RENAME_DIR / (str(uuid.UUID(operation_id)) + '.json')


def save(operation):
    RENAME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _path(operation['operation_id'])
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(operation, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load(operation_id):
    return json.loads(_path(operation_id).read_text())


def latest(mind_id):
    records = [json.loads(path.read_text()) for path in RENAME_DIR.glob('*.json')]
    records = [row for row in records if row['mind_id'] == mind_id]
    return max(records, key=lambda row: row['created_at']) if records else None


def reserved(name):
    return any(row['status'] not in FINAL and name in (row['old'], row['new'])
               for row in (json.loads(path.read_text()) for path in RENAME_DIR.glob('*.json')))


def public(operation):
    return {key: operation[key] for key in ('operation_id', 'mind_id', 'old', 'new', 'status', 'detail', 'hitl_request_id') if key in operation}


async def _request(method, url, headers, body=None, params=None, timeout=15):
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=timeout, follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, json=body, params=params)
        if response.status_code not in (200, 201, 202):
            raise RenameServiceError(response.status_code)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Rename service returned an invalid result')
        return result
    except (httpx.HTTPError, json.JSONDecodeError):
        raise ValueError('A rename service could not be reached; the operation can be recovered') from None


#: The tools hold a call that needs a person's yes open for up to two
#: minutes (HITL_WAIT_SECONDS), so a call that may be held is waited on a
#: little longer than that.
HELD_CALL_TIMEOUT = 310


async def _tools(operation, method, suffix=''):
    import main
    base = main._hive_tools_base_url().rstrip('/')
    url = f'{base}/docker/mind-renames/{operation["operation_id"]}{suffix}'
    return await _request(method, url, await _requester_headers(operation.get('requester')), params={'path': operation['project_path']},
                          timeout=HELD_CALL_TIMEOUT if suffix == '/authorize' else 180)


async def _requester_headers(username):
    """The gateway's bearer for a rename: the key of the person who asked for
    it, who is on the operation record. This site holds no key of its own
    (feature 834, story 905). No key to be had: the step waits as recoverable."""
    key = await holder_credentials.user_key(str(username or ''))
    if not key:
        raise ValueError('The key of the person who asked for this rename could not be found')
    return {'Authorization': 'Bearer ' + key}


async def _broker(operation, action):
    service = holder_credentials.SERVICES['comms']
    headers = await _requester_headers(operation.get('requester'))
    return await _request('POST', f'{service.url().rstrip("/")}/broker/minds/{operation["mind_id"]}/rename/{action}',
                          headers, {'operation_id': operation['operation_id']})


async def _runtime(operation, name):
    """Verify actual runtime identity before committing or releasing recovery."""
    target = f'http://{name}:8420'
    # The registry still names the mind by its old name until the rename is
    # committed, so no row claims the new address yet: the mind's own key is
    # looked up by the names it is known by, not by the address.
    key = None
    for known in (operation['old'], operation['new']):
        key = await asyncio.to_thread(configuration_data.key_held_for, known)
        if key:
            break
    if not key:
        raise ValueError('This Mind’s runtime credential could not be read')
    headers = {'Authorization': f'Bearer {key}'}
    for attempt in range(30):
        try:
            result = await _request('GET', target + '/runtime', headers)
        except ValueError:
            if attempt == 29:
                raise
            await asyncio.sleep(0.5)
            continue
        configuration = result.get('configuration', {})
        if configuration.get('mind_id') != operation['mind_id'] or configuration.get('name') != name:
            raise ValueError('The restarted runtime does not have this Mind’s expected identity')
        return configuration


async def request_rename(mind_id, new, user):
    new = str(new or '').strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,31}', new):
        raise ValueError('Name must be 2 to 32 lowercase letters, numbers, or hyphens')
    async with _REQUEST_LOCK:
        row = await core.get_mind(mind_id)
        if not row:
            raise ValueError('Mind not found')
        existing = latest(mind_id)
        if existing and existing['status'] == 'success' and existing['new'] == new and row['name'] == new:
            return public(existing)
        if existing and existing['status'] not in FINAL:
            if existing['new'] != new:
                raise ValueError('This Mind already has a pending rename')
            schedule(existing['operation_id'])
            return public(existing)
        if reserved(new) or reserved(row['name']):
            raise ValueError('This name is reserved by another rename')
        if mind_creation.is_open(new) or mind_creation.is_open(row['name']):
            raise ValueError('Finish this name’s pending creation before renaming')
        await core.preflight_mind_rename(mind_id, row['name'], new)
        if not configuration_data.internal_mind_exists(row['name']):
            raise ValueError('Rename is available for Minds running in this Hive’s local stack')
        if os.getenv('MIND_COMPOSE_TARGETS'):
            raise ValueError('Rename requires a Mind managed by this Hive’s local stack')
        if await asyncio.to_thread(configuration_data.name_is_a_host_already, new):
            raise ValueError('That name already addresses another host on the Hive network')
        operation = {'operation_id': str(uuid.uuid4()), 'mind_id': mind_id,
                     'old': row['name'], 'new': new, 'owner': row['owner'],
                     'requester': user['username'], 'created_at': time.time(),
                     'project_path': mind_mounts.hive_mind_project_path(),
                     'status': 'reserving', 'detail': 'Checking the rename prerequisites'}
        # Refuse unsupported gateway versions before touching any live state.
        service = holder_credentials.SERVICES['comms']
        capabilities = await _request('GET', service.url().rstrip('/') + '/broker/minds/rename/capabilities',
                                      await _requester_headers(user['username']))
        if capabilities.get('version') != 1:
            raise ValueError('The gateway needs rename support before this Mind can be renamed')
        import main
        save(operation)
        try:
            await _request('POST', main._hive_tools_base_url().rstrip('/') + '/docker/mind-renames/reserve',
                           await _requester_headers(user['username']), {key: operation[key] for key in ('operation_id', 'mind_id', 'old', 'new')},
                           {'path': operation['project_path']})
        except ValueError:
            await _recover(operation, 'The rename reservation could not finish')
            raise
        schedule(operation['operation_id'])
        return public(operation)


async def _recover(operation, detail):
    operation.update(status='recovering', detail=detail)
    save(operation)
    try:
        try:
            tools = await _tools(operation, 'GET')
        except RenameServiceError as exc:
            if exc.status == 404 and not operation.get('gate_prepared'):
                operation.update(status='failed', detail=detail + '. The original Mind was unchanged')
                save(operation)
                return public(operation)
            raise
        if tools['stage'] == 'committed':
            # Confirmation landed before the website lost its response. This
            # is a completed rename, never a reason to undo somebody’s Mind.
            row = await core.get_mind(operation['mind_id'])
            if row['name'] != operation['new']:
                raise ValueError('The confirmed host rename disagrees with the registry')
            await _broker(operation, 'release')
            operation.update(status='success', detail='Renamed. Everything else was preserved')
        else:
            row = await core.get_mind(operation['mind_id'])
            if row['name'] == operation['new']:
                async with provider_data._LOCK:
                    await core.rename_mind(operation['mind_id'], operation['new'], operation['old'])
            await _tools(operation, 'POST', '/rollback')
            if tools['stage'] not in ('reserved', 'authorized', 'cancelled'):
                await _runtime(operation, operation['old'])
            await _broker(operation, 'release')
            operation.update(status='failed', detail=detail + '. The original Mind was restored')
    except (ValueError, OSError, KeyError):
        operation.update(status='recovery_required', detail='Rename recovery is pending. The Mind remains protected; retry recovery from this page')
    save(operation)
    return public(operation)


async def advance(operation_id):
    """A resumable step. Host status resolves lost responses and restarts."""
    async with _STEP_LOCKS.setdefault(operation_id, asyncio.Lock()):
        operation = load(operation_id)
        if operation['status'] in FINAL:
            return public(operation)
        if operation['status'] in ('recovering', 'recovery_required'):
            return await _recover(operation, 'The rename could not finish')
        try:
            tools = await _tools(operation, 'GET')
            stage = tools['stage']
            if stage == 'reserved':
                if not operation.get('hitl_request_id'):
                    # The tools hold this call open until a person approves it
                    # (or answer no on denial or no answer in time).
                    operation.update(status='waiting_approval',
                                     detail='Approve this Mind’s rename in Hive Tools')
                    save(operation)
                    try:
                        result = await _tools(operation, 'POST', '/authorize')
                    except RenameServiceError as exc:
                        if exc.status in (403, 408):
                            return await _recover(operation, 'The rename was not approved')
                        raise
                    if result.get('hitl_request_id'):
                        operation.update(hitl_request_id=result['hitl_request_id'], status='waiting_approval',
                                         detail='Approve this Mind’s rename in Hive Tools')
                        import console_access
                        await console_access.record_operation(operation['hitl_request_id'], operation['requester'])
                        save(operation)
                        return public(operation)
                    stage = result.get('stage', stage)
                else:
                    import main
                    result = await _request('GET', main._hive_tools_base_url().rstrip('/') + '/hitl/' + operation['hitl_request_id'], await _requester_headers(operation.get('requester')))
                    if result.get('status') in ('denied', 'expired'):
                        return await _recover(operation, 'The rename was not approved')
                    return public(operation)
            if stage == 'authorized':
                prepared = await _broker(operation, 'prepare')
                if prepared.get('status') != 'prepared':
                    operation.update(status='waiting_idle', detail=prepared.get('detail') or 'Waiting for this Mind to become idle')
                    save(operation)
                    return public(operation)
                operation.update(status='transitioning', detail='Moving this Mind and restarting its runtime')
                operation['gate_prepared'] = True
                save(operation)
                tools = await _tools(operation, 'POST', '/continue')
                stage = tools['stage']
            if stage == 'running':
                await _runtime(operation, operation['new'])
                operation.update(status='committing', detail='Preserving this Mind’s identity and references')
                save(operation)
                async with provider_data._LOCK:
                    await core.rename_mind(operation['mind_id'], operation['old'], operation['new'])
                operation.update(status='confirming', detail='Verifying the completed rename')
                save(operation)
                await _tools(operation, 'POST', '/confirm')
                stage = 'committed'
            if stage == 'committed':
                row = await core.get_mind(operation['mind_id'])
                if row['name'] != operation['new']:
                    raise ValueError('The renamed runtime disagrees with the registry')
                await _broker(operation, 'release')
                operation.update(status='success', detail='Renamed. Everything else was preserved')
                save(operation)
                return public(operation)
            return await _recover(operation, 'The host transition was interrupted')
        except (ValueError, OSError, KeyError):
            return await _recover(operation, 'The rename could not finish')


def schedule(operation_id):
    if operation_id in _TASKS and not _TASKS[operation_id].done():
        return
    async def run():
        while True:
            result = await advance(operation_id)
            if result['status'] in FINAL or result['status'] == 'recovery_required':
                return
            await asyncio.sleep(3)
    task = asyncio.create_task(run())
    _TASKS[operation_id] = task
    task.add_done_callback(lambda done: _TASKS.pop(operation_id, None))


async def cancel(operation_id):
    async with _STEP_LOCKS.setdefault(operation_id, asyncio.Lock()):
        operation = load(operation_id)
        if operation['status'] not in ('reserving', 'waiting_approval', 'waiting_idle'):
            raise ValueError('This rename has already progressed; use its recovery status')
        result = await _recover(operation, 'The rename was cancelled')
        if result['status'] == 'failed':
            operation = load(operation_id)
            operation['status'] = 'cancelled'
            save(operation)
            return public(operation)
        return result


def recover_pending():
    for path in RENAME_DIR.glob('*.json'):
        operation = json.loads(path.read_text())
        if operation['status'] not in FINAL:
            schedule(operation['operation_id'])


async def stop():
    tasks = list(_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
