"""Bounded, on-demand diagnostics over components' existing HTTP credentials."""
import asyncio

import httpx


async def probe(client, name, url, headers, *, feed=True):
    result = {'name': name, 'status': 'ok', 'errors': []}
    try:
        response = await client.get(url.rstrip('/') + ('/diagnostics/errors' if feed else '/health'),
                                    headers=headers)
        if feed and response.status_code == 404:
            result.update(status='unsupported', detail='Error feed unavailable; update this component.')
        elif response.status_code >= 400:
            result.update(status='refused' if response.status_code in (401, 403) else 'error',
                          detail=f'Component returned HTTP {response.status_code}')
        elif feed:
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get('errors'), list):
                raise ValueError('Invalid component error feed')
            result.update(errors=body['errors'][:100], started_at=body.get('started_at'))
        else:
            result.update(status='health-only', detail='Health check succeeded; runtime error feed is not available.')
    except (httpx.HTTPError, ValueError):
        result.update(status='unreachable', detail='Could not read component diagnostics (connection, timeout, or invalid response).')
    return result


async def collect(targets, local):
    async with httpx.AsyncClient(timeout=4.0, limits=httpx.Limits(max_connections=10)) as client:
        components = await asyncio.gather(*(probe(client, **target) for target in targets))
    return {'components': [{'name': 'Hive site', 'status': 'ok', **local}, *components]}
