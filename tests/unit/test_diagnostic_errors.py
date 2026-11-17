"""P2: mind and nervous-system logs retain the actual bounded failure detail."""
import logging
import time

import hive_logging as nervous_logging
import pytest

from core import hive_logging as mind_logging


@pytest.mark.parametrize('module', [mind_logging, nervous_logging])
def test_terminal_failure_is_retained_with_its_exception(module):
    record = logging.LogRecord('mind.pty', logging.ERROR, '', 0, 'Failed to open terminal', (), None)
    try:
        raise RuntimeError('the inference proxy declares no context window for gpt-5.6-sol')
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    module.HiveJsonFormatter().format(record)
    assert module.recent_errors()['errors'][0]['detail'] == 'the inference proxy declares no context window for gpt-5.6-sol'


@pytest.mark.parametrize('module', [mind_logging, nervous_logging])
def test_error_feed_is_bounded_recent_and_redacts_credentials(module, monkeypatch):
    monkeypatch.setenv('COMMS_ADMIN_BEARER_TOKEN', 'a-fake-test-credential')
    monkeypatch.setenv('OPENAI_API_KEY', 'a-fake-provider-key')
    formatter = module.HiveJsonFormatter()
    for index in range(110):
        formatter.format(logging.LogRecord('mind', logging.ERROR, '', 0, f'failure {index} a-fake-test-credential', (), None))
    old = logging.LogRecord('mind', logging.ERROR, '', 0, 'expired error', (), None)
    old.created = time.time() - 3601
    formatter.format(old)
    feed = module.recent_errors()['errors']
    assert len(feed) <= 100
    assert feed[0]['message'] == 'failure 109 [REDACTED]'
    assert all('expired error' not in item['message'] for item in feed)
    formatter.format(logging.LogRecord('mind', logging.ERROR, '', 0,
                                      'a-fake-provider-key postgresql://user:db-password@host/db', (), None))
    assert module.recent_errors()['errors'][0]['message'] == '[REDACTED] postgresql://user:[REDACTED]@host/db'


@pytest.mark.parametrize('module', [mind_logging, nervous_logging])
def test_a_very_long_error_is_cut_before_it_is_scrubbed_and_shows_no_part_of_a_credential(module, monkeypatch):
    monkeypatch.setenv('COMMS_ADMIN_BEARER_TOKEN', 'a-fake-test-credential')
    # One unbroken run, the shape of a dumped blob: scrubbing all of it took
    # 6.7 seconds on the logging thread before the cut was moved first.
    message = 'x' * 4090 + 'a-fake-test-credential' + 'deadbeef' * 12_500
    started = time.perf_counter()
    module.HiveJsonFormatter().format(logging.LogRecord('mind', logging.ERROR, '', 0, message, (), None))
    elapsed = time.perf_counter() - started
    assert module.recent_errors()['errors'][0]['message'] == 'x' * 4090 + '[REDAC'
    assert elapsed < 1.0


def test_mind_error_feed_requires_admin_and_returns_terminal_exception(monkeypatch):
    from starlette.testclient import TestClient

    from minds import mind_server

    monkeypatch.setenv('MIND_ADMIN_TOKEN', 'diagnostics-test-admin')
    client = TestClient(mind_server.app)
    assert client.get('/diagnostics/errors').status_code == 401
    try:
        raise RuntimeError('no context window for gpt-5.6-sol')
    except RuntimeError:
        mind_server.log.exception('Failed to open terminal')
    response = client.get('/diagnostics/errors', headers={'Authorization': 'Bearer diagnostics-test-admin'})
    assert response.status_code == 200
    assert any(item['detail'] == 'no context window for gpt-5.6-sol' for item in response.json()['errors'])
