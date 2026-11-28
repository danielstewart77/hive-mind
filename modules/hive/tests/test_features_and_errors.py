"""P1: saved navigation choices; P2: real failures reach the errors page."""
import logging
import time

import httpx

import hive_errors
import hive_logging


def test_saved_features_hide_navigation_and_keep_settings(builtin_console):
    client = builtin_console.admin()
    response = client.put('/api/settings/features', json={'graph': False, 'setup': False, 'usage': False, 'roles': False})
    assert response.status_code == 200
    page = client.get('/settings/features')
    assert page.status_code == 200
    nav = page.text.split('aria-label="Main navigation"')[1].split('</aside>')[0]
    assert 'href="/graph"' not in nav
    assert 'href="/setup"' not in nav
    assert 'href="/usage"' not in nav
    assert 'href="/roles"' not in nav
    assert 'href="/settings/general"' in nav
    assert client.get('/api/settings/features').json()['enabled']['graph'] is False
    assert client.put('/api/settings/features', json={'graph': True}).status_code == 200
    assert 'href="/graph"' in client.get('/settings/features').text


def test_feature_choices_belong_to_the_signed_in_account(builtin_console):
    admin = builtin_console.admin()
    member = builtin_console.settled_user(admin, 'member', 'member-test-password')
    assert member.put('/api/settings/features', json={'minds': False}).status_code == 200
    assert member.get('/settings/features').status_code == 200
    assert member.get('/api/settings/features').json()['enabled']['minds'] is False
    assert admin.get('/api/settings/features').json()['enabled']['minds'] is True
    assert member.get('/api/hive-errors').status_code == 403


def test_hive_errors_endpoint_reports_a_mind_failure(builtin_console, monkeypatch):
    client = builtin_console.admin()
    monkeypatch.setenv('COMMS_ADMIN_BEARER_TOKEN', 'stand-in-admin')
    original_client = httpx.AsyncClient
    feed = {'started_at': '2026-11-13T18:00:00Z', 'errors': [
        {'message': 'Failed to open terminal', 'detail': 'no context window for gpt-5.6-sol'}]}
    asked = {}
    def transport(request):
        if request.url.path == '/broker/minds':
            return httpx.Response(200, json=[
                {'id': 'atlas-id', 'name': 'Atlas', 'owner': 'daniel', 'gateway_url': 'http://atlas:8420'},
                {'id': 'kid-id', 'name': 'Kidbox', 'owner': 'sloan', 'gateway_url': 'http://kidbox:8433'}])
        asked.setdefault(request.url.host, []).append(
            (request.url.path, request.headers.get('authorization')))
        return httpx.Response(200, json=feed, headers={'x-hive-forwarded': '1'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(transport)))
    result = client.get('/api/hive-errors')
    assert result.status_code == 200
    atlas = next(c for c in result.json()['components'] if c['name'] == 'Atlas')
    assert atlas['errors'][0]['detail'] == 'no context window for gpt-5.6-sol'
    assert client.get('/hive-errors').status_code == 200
    # Every mind is asked through the gateway, never at its own address, with
    # the signed-in person's own key and never the gateway administrator's.
    assert 'atlas' not in asked and 'kidbox' not in asked
    forwarded = [(p, a) for calls in asked.values() for p, a in calls if '/forward/' in p]
    assert any(p.startswith('/broker/minds/atlas-id/forward/') for p, _ in forwarded)
    assert len({a for _, a in forwarded}) == 1
    assert all(a.startswith('Bearer ') and a != 'Bearer stand-in-admin' for _, a in forwarded)


async def test_terminal_start_exception_reaches_component_error_report():
    record = logging.LogRecord('mind.pty', logging.ERROR, '', 0,
                               'Failed to open terminal', (), None)
    try:
        raise RuntimeError('the inference proxy declares no context window for gpt-5.6-sol')
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    hive_logging.HiveJsonFormatter().format(record)
    feed = hive_logging.recent_errors()
    async def transport(request):
        return httpx.Response(200, json=feed)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await hive_errors.probe(client, 'Atlas', 'http://atlas', {})
    assert result['status'] == 'ok'
    assert any('no context window for gpt-5.6-sol' in item['detail']
               for item in result['errors'])


async def test_unavailable_error_feed_is_reported_without_hiding_other_components():
    async def transport(request):
        if request.url.host == 'old':
            return httpx.Response(404)
        raise httpx.ConnectError('unreachable', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        old = await hive_errors.probe(client, 'Old mind', 'http://old', {})
        down = await hive_errors.probe(client, 'Down mind', 'http://down', {})
    assert old['status'] == 'unsupported'
    assert down['status'] == 'unreachable'


def test_a_very_long_error_is_scrubbed_quickly_and_shows_no_part_of_a_credential(monkeypatch):
    monkeypatch.setenv('COMMS_ADMIN_BEARER_TOKEN', 'a-fake-test-credential')
    # One unbroken run, the shape of a dumped blob: scrubbing it took 6.7
    # seconds on the logging thread when the address pattern was quadratic.
    message = 'x' * 4090 + 'a-fake-test-credential' + 'deadbeef' * 12_500
    started = time.perf_counter()
    hive_logging.HiveJsonFormatter().format(logging.LogRecord('site', logging.ERROR, '', 0, message, (), None))
    elapsed = time.perf_counter() - started
    assert hive_logging.recent_errors()['errors'][0]['message'] == 'x' * 4090 + '[REDAC'
    assert elapsed < 1.0

def test_a_credential_the_scrub_window_cuts_in_two_never_reaches_the_feed_in_part(monkeypatch):
    monkeypatch.setenv('COMMS_ADMIN_BEARER_TOKEN', 'a-fake-test-credential')
    harmless = 'failure ' + 'y ' * 1500
    # The long bearer value is replaced by ten characters, which pulls the end
    # of the window, and the half credential lying there, to the front.
    lead = harmless + 'bearer '
    message = lead + 'A' * (hive_logging._SCRUB_WINDOW - len(lead) - 22) + ' ' + 'x' * 10 + 'a-fake-test-credential'
    hive_logging.HiveJsonFormatter().format(logging.LogRecord('site', logging.ERROR, '', 0, message, (), None))
    kept = hive_logging.recent_errors()['errors'][0]['message']
    assert len(kept) > 500
    assert kept == harmless[:len(kept)]

def test_a_bearer_value_and_a_named_secret_are_scrubbed_from_the_message_and_the_exception():
    record = logging.LogRecord('site', logging.ERROR, '', 0,
                               'refused with Authorization: Bearer abc.def-123 for the proxy', (), None)
    try:
        raise RuntimeError('login failed, password=hunter2; api_key: zz99, retrying')
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    hive_logging.HiveJsonFormatter().format(record)
    newest = hive_logging.recent_errors()['errors'][0]
    assert newest['message'] == 'refused with Authorization: Bearer [REDACTED] for the proxy'
    assert newest['detail'] == 'login failed, password=[REDACTED]; api_key: [REDACTED], retrying'
