"""Durable, per-Mind quiescence for a recoverable host rename."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

from hive_data import core
from starlette.requests import Request
from starlette.responses import JSONResponse

_LOCK = asyncio.Lock()
_ACTIVE: dict[str, int] = {}


async def identity(key: str) -> str:
    row = await core.get_mind_by_id_or_name(key)
    return str(row['mind_id']) if row else key


async def state(mind_id: str) -> dict:
    row = await core.get_setting('mind.rename.' + mind_id)
    return json.loads(row['value']) if row and row['value'] else {}


async def check(mind_id: str) -> None:
    if await state(await identity(mind_id)):
        raise ValueError('This Mind is being renamed. Retry when the rename finishes')


@asynccontextmanager
async def activity(mind_id: str):
    mind_id = await identity(mind_id)
    async with _LOCK:
        await check(mind_id)
        _ACTIVE[mind_id] = _ACTIVE.get(mind_id, 0) + 1
    try:
        yield
    finally:
        async with _LOCK:
            _ACTIVE[mind_id] -= 1


async def prepare(mind_id: str, operation_id: str, turn_locks: dict) -> dict:
    """Claim the idle boundary; never stop an active turn or held terminal."""
    mind_id = await identity(mind_id)
    async with _LOCK:
        current = await state(mind_id)
        if current:
            if current.get('operation_id') != operation_id:
                raise ValueError('Another rename already holds this Mind')
            return {'status': 'prepared'}
        rows = await core.list_sessions(mind_id=mind_id, private=True)
        locks = [turn_locks.setdefault(row['id'], asyncio.Lock()) for row in rows]
        if _ACTIVE.get(mind_id) or any(lock.locked() for lock in locks):
            return {'status': 'waiting', 'detail': 'Waiting for this Mind’s active turn to finish'}
        if any(row.get('terminal_held') or (str(row.get('surface', '')).split(':', 1)[0] == 'terminal' and row.get('status') not in ('closed', 'suspended')) for row in rows):
            return {'status': 'waiting', 'detail': 'Release or suspend this Mind’s terminal before renaming'}
        # Acquiring free locks does not yield. Sends take these same locks
        # before checking the durable gate, so no turn can pass this boundary.
        for lock in locks:
            await lock.acquire()
        try:
            await core.put_setting('mind.rename.' + mind_id,
                                   json.dumps({'operation_id': operation_id}), time.time())
        finally:
            for lock in locks:
                lock.release()
        return {'status': 'prepared'}


async def release(mind_id: str, operation_id: str) -> dict:
    async with _LOCK:
        current = await state(mind_id)
        if current and current.get('operation_id') != operation_id:
            raise ValueError('This rename does not own the maintenance gate')
        await core.put_setting('mind.rename.' + mind_id, '', time.time())
    return {'status': 'released'}


class MaintenanceMiddleware:
    """Cover mutation requests and terminal WebSockets for their whole lifetime."""
    def __init__(self, app, session_mind):
        self.app, self.session_mind = app, session_mind

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        mutating = scope.get('method') in ('POST', 'PUT', 'PATCH', 'DELETE')
        terminal = scope.get('type') == 'websocket' and path.endswith('/attach')
        if not path.startswith('/sessions') or not (mutating or terminal):
            return await self.app(scope, receive, send)
        # Hooks reporting the final turn and release must still land while
        # draining. They cannot start a harness or dispatch another turn.
        if path.endswith(('/release', '/suspend', '/pty-text', '/pty-activity', '/harness-state')) or path in ('/sessions/record-turn', '/sessions/arm-rotation'):
            return await self.app(scope, receive, send)
        key = None
        parts = path.strip('/').split('/')
        if len(parts) > 1:
            key = await self.session_mind(parts[1])
        if not key and scope.get('type') == 'http':
            request = Request(scope, receive)
            body = await request.body()
            try:
                key = json.loads(body).get('mind_id') if body else None
            except (ValueError, AttributeError):
                pass
            delivered = False
            original_receive = receive
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await original_receive()
            receive = replay
        if not key:
            return await self.app(scope, receive, send)
        lease = activity(key)
        try:
            await lease.__aenter__()
        except ValueError as exc:
            if scope.get('type') == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            else:
                await JSONResponse({'detail': str(exc)}, status_code=409)(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await lease.__aexit__(None, None, None)
