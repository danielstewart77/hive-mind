"""runtime.yaml is a mind's durable configuration, read and written here.

The console configures every mind the same way — over HTTP against these
routes — whether the mind runs in this stack or on another host, so the
containerized harnesses mount them too.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import runtime_api

RUNTIME = """\
# A mind's runtime configuration.
name: example
mind_id: 565e5a66-d20c-4266-872a-3268c4c894fc
gateway_url: http://example:8420
remote: false

harness: claude_cli
provider: anthropic
# The model every new conversation starts on.
default_model: sonnet
resume_policy: always
"""


@pytest.fixture()
def runtime_file(tmp_path):
    path = tmp_path / "runtime.yaml"
    path.write_text(RUNTIME)
    return path


class TestLoad:
    def test_reads_the_file(self, runtime_file):
        assert runtime_api.load_runtime(runtime_file)["default_model"] == "sonnet"

    def test_public_view_drops_unlisted_fields(self, runtime_file):
        runtime_file.write_text(RUNTIME + "internal_note: not for the console\n")
        public = runtime_api.public_runtime(runtime_file)
        assert "internal_note" not in public
        assert public["harness"] == "claude_cli"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError):
            runtime_api.load_runtime(tmp_path / "absent.yaml")


class TestUpdateDefaultModel:
    def test_writes_the_new_model(self, runtime_file):
        assert (
            runtime_api.update_default_model(runtime_file, "opus")["default_model"]
            == "opus"
        )
        assert runtime_api.load_runtime(runtime_file)["default_model"] == "opus"

    def test_preserves_comments_and_other_fields(self, runtime_file):
        runtime_api.update_default_model(runtime_file, "opus")
        text = runtime_file.read_text()
        assert "# The model every new conversation starts on." in text
        assert "resume_policy: always" in text

    def test_accepts_an_ollama_tag(self, runtime_file):
        model = "qwen3:30b-a3b-instruct-2507-q4_K_M"
        assert (
            runtime_api.update_default_model(runtime_file, model)["default_model"]
            == model
        )

    @pytest.mark.parametrize("model", ["", "opus; rm -rf /", "opus\nname: evil"])
    def test_rejects_an_unusable_model_name(self, runtime_file, model):
        with pytest.raises(ValueError):
            runtime_api.update_default_model(runtime_file, model)
        assert runtime_api.load_runtime(runtime_file)["default_model"] == "sonnet"

    def test_leaves_no_temporary_files_behind(self, runtime_file):
        runtime_api.update_default_model(runtime_file, "opus")
        assert [p.name for p in runtime_file.parent.iterdir()] == ["runtime.yaml"]


class TestRegistrationPayload:
    def test_describes_the_broker_row(self, runtime_file):
        assert runtime_api.registration_payload(runtime_file) == {
            "mind_id": "565e5a66-d20c-4266-872a-3268c4c894fc",
            "name": "example",
            "gateway_url": "http://example:8420",
            "model": "sonnet",
            "harness": "claude_cli",
        }

    def test_tracks_an_edit(self, runtime_file):
        runtime_api.update_default_model(runtime_file, "opus")
        assert runtime_api.registration_payload(runtime_file)["model"] == "opus"

    def test_incomplete_file_raises_rather_than_registering_a_half_mind(
        self, runtime_file
    ):
        runtime_file.write_text("name: example\ndefault_model: opus\n")
        with pytest.raises(ValueError) as exc:
            runtime_api.registration_payload(runtime_file)
        assert "mind_id" in str(exc.value)


class TestAdminToken:
    def test_prefers_the_dedicated_token(self, monkeypatch):
        monkeypatch.setenv("MIND_ADMIN_TOKEN", "mind-token")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "gateway-token")
        assert runtime_api.admin_token() == "mind-token"

    def test_falls_back_to_the_gateway_bearer(self, monkeypatch):
        monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "gateway-token")
        assert runtime_api.admin_token() == "gateway-token"

    def test_no_token_configured_is_empty_not_open(self, monkeypatch):
        monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
        assert runtime_api.admin_token() == ""


@pytest.fixture()
def client(runtime_file, monkeypatch):
    monkeypatch.setenv("MIND_ADMIN_TOKEN", "s3cret")
    app = FastAPI()
    import logging

    runtime_api.install_runtime_routes(
        app, path=runtime_file, mind_id="mind-1", log=logging.getLogger("test")
    )
    return TestClient(app, raise_server_exceptions=False)


class TestRoutes:
    def test_get_reports_the_configuration(self, client):
        body = client.get("/runtime").json()["configuration"]
        assert body["default_model"] == "sonnet"
        assert body["provider"] == "anthropic"

    def test_patch_writes_the_model_to_disk(self, client, runtime_file):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
        assert response.json()["configuration"]["default_model"] == "opus"
        assert "default_model: opus" in runtime_file.read_text()

    def test_patch_rejects_a_missing_token(self, client, runtime_file):
        assert client.patch("/runtime", json={"default_model": "opus"}).status_code == 401
        assert "default_model: sonnet" in runtime_file.read_text()

    def test_patch_refuses_when_no_token_is_configured(
        self, client, runtime_file, monkeypatch
    ):
        monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
        assert client.patch("/runtime", json={"default_model": "opus"}).status_code == 503

    def test_patch_rejects_a_bad_model_name(self, client, runtime_file):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus; rm -rf /"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 400
        assert "default_model: sonnet" in runtime_file.read_text()


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestRegisterWithBroker:
    async def test_posts_runtime_yaml_to_the_broker(self, runtime_file, monkeypatch):
        monkeypatch.setenv("COMMS_URL", "http://hive-comms:8424")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")
        posted: dict = {}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, json, headers, timeout, **kwargs):
                posted.update(url=url, payload=json, headers=headers)
                return _Response()

        import logging

        with patch("aiohttp.ClientSession", _Session):
            await runtime_api.register_with_broker(
                runtime_file, mind_name="example", mind_id="mind-1",
                log=logging.getLogger("test"),
            )

        assert posted["url"] == "http://hive-comms:8424/broker/minds"
        assert posted["headers"]["Authorization"] == "Bearer admin"
        assert posted["payload"]["model"] == "sonnet"
        assert posted["payload"]["gateway_url"] == "http://example:8420"

    async def test_registers_an_edited_model_after_a_restart(
        self, runtime_file, monkeypatch
    ):
        monkeypatch.setenv("COMMS_URL", "http://hive-comms:8424")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")
        runtime_api.update_default_model(runtime_file, "opus")
        posted: dict = {}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, json, headers, timeout, **kwargs):
                posted.update(payload=json)
                return _Response()

        import logging

        with patch("aiohttp.ClientSession", _Session):
            await runtime_api.register_with_broker(
                runtime_file, mind_name="example", mind_id="mind-1",
                log=logging.getLogger("test"),
            )

        assert posted["payload"]["model"] == "opus"

    async def test_unreachable_broker_does_not_stop_the_mind(
        self, runtime_file, monkeypatch
    ):
        monkeypatch.setenv("COMMS_URL", "http://hive-comms:8424")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")

        class _Session:
            async def __aenter__(self):
                raise OSError("connection refused")

            async def __aexit__(self, *exc):
                return False

        import logging

        with patch("aiohttp.ClientSession", _Session):
            await runtime_api.register_with_broker(
                runtime_file, mind_name="example", mind_id="mind-1",
                log=logging.getLogger("test"),
            )  # no raise

    async def test_no_comms_configured_is_a_no_op(self, runtime_file, monkeypatch):
        monkeypatch.delenv("COMMS_URL", raising=False)
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")
        import logging

        with patch(
            "aiohttp.ClientSession", side_effect=AssertionError("should not connect")
        ):
            await runtime_api.register_with_broker(
                runtime_file, mind_name="example", mind_id="mind-1",
                log=logging.getLogger("test"),
            )


class _StatusResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session_returning(statuses):
    """An aiohttp.ClientSession stand-in that answers from a status script.

    A status of None raises OSError instead — an unreachable broker.
    """
    script = list(statuses)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json, headers, timeout, **kwargs):
            status = script.pop(0) if script else script_default
            if status is None:
                raise OSError("connection refused")
            return _StatusResponse(status)

    script_default = script[-1] if script else 200
    return _Session


class TestRegistrationLoop:
    """One test per shipped requirement; the loop is the boot-race fix."""

    @pytest.fixture(autouse=True)
    def _comms_env(self, monkeypatch):
        monkeypatch.setenv("COMMS_URL", "http://hive-comms:8424")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")

    async def test_registers_once_the_broker_comes_up_without_a_restart(
        self, runtime_file
    ):
        """Req 1: boot wires the loop in; attempts fail while comms is down
        and succeed the moment it returns — waiting retry delays, not a
        restart, in between."""
        import asyncio
        import logging
        from unittest.mock import AsyncMock

        # The race fix is dead code unless startup actually schedules the
        # loop — deleting the ensure_future line must fail this test.
        from minds.harness import claude_cli, codex_cli

        for harness in (claude_cli, codex_cli):
            scheduled = AsyncMock()
            with patch.object(runtime_api, "registration_loop", scheduled), \
                    patch.object(harness, "_fetch_secrets_on_startup", AsyncMock()):
                with TestClient(harness.app):
                    pass
            assert scheduled.called, f"{harness.NAME} never starts the loop"

        outcomes: list[str] = []
        sleeps: list[float] = []
        real = runtime_api.register_with_broker

        async def recording_register(*args, **kw):
            outcome = await real(*args, **kw)
            outcomes.append(outcome)
            return outcome

        async def recording_sleep(seconds):
            sleeps.append(seconds)
            if "registered" in outcomes:
                raise asyncio.CancelledError

        with patch(
            "aiohttp.ClientSession", _session_returning([None, None, 200])
        ), patch.object(runtime_api, "register_with_broker", recording_register):
            with pytest.raises(asyncio.CancelledError):
                await runtime_api.registration_loop(
                    runtime_file, mind_name="example", mind_id="mind-1",
                    log=logging.getLogger("test"), sleep=recording_sleep,
                )

        assert outcomes == ["retry", "retry", "registered"]
        # Retry waits between the failures, heartbeat wait after the success
        # — a loop that ignores outcomes sleeps the wrong sequence.
        assert sleeps == [1.0, 2.0, 300.0]

    async def test_retry_delays_grow_and_never_exceed_sixty_seconds(
        self, runtime_file
    ):
        """Req 2: backoff doubles per failure and caps at 60s."""
        import asyncio
        import logging

        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 9:
                raise asyncio.CancelledError

        with patch("aiohttp.ClientSession", _session_returning([None])):
            with pytest.raises(asyncio.CancelledError):
                await runtime_api.registration_loop(
                    runtime_file, mind_name="example", mind_id="mind-1",
                    log=logging.getLogger("test"), sleep=fake_sleep,
                )

        assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]

    async def test_a_rejected_registration_stops_and_is_logged_once(
        self, runtime_file, caplog
    ):
        """Req 3: a 4xx means the payload or token is wrong; retrying resends it."""
        import logging

        caplog.set_level(logging.INFO, logger="test-rejected")
        with patch("aiohttp.ClientSession", _session_returning([401])):
            await runtime_api.registration_loop(
                runtime_file, mind_name="example", mind_id="mind-1",
                log=logging.getLogger("test-rejected"), sleep=None,
            )  # returns rather than looping — a looping loop would hang here

        rejected = [r for r in caplog.records if "mind.register.rejected" in r.message]
        assert len(rejected) == 1

    async def test_a_broker_server_error_is_retried(self, runtime_file):
        """Req 4: a 5xx is the broker's problem, not the payload's."""
        import asyncio
        import logging

        outcomes: list[str] = []
        real = runtime_api.register_with_broker

        async def recording_register(*args, **kw):
            outcome = await real(*args, **kw)
            outcomes.append(outcome)
            return outcome

        sleeps: list[float] = []

        async def recording_sleep(seconds):
            sleeps.append(seconds)
            if "registered" in outcomes:
                raise asyncio.CancelledError

        with patch(
            "aiohttp.ClientSession", _session_returning([503, 200])
        ), patch.object(runtime_api, "register_with_broker", recording_register):
            with pytest.raises(asyncio.CancelledError):
                await runtime_api.registration_loop(
                    runtime_file, mind_name="example", mind_id="mind-1",
                    log=logging.getLogger("test"), sleep=recording_sleep,
                )

        assert outcomes == ["retry", "registered"]
        assert sleeps == [1.0, 300.0]

    async def test_the_heartbeat_reregisters_after_five_minutes(self, runtime_file):
        """Req 5: success is followed by another registration a heartbeat later."""
        import asyncio
        import logging

        registrations = 0
        sleeps: list[float] = []
        real = runtime_api.register_with_broker

        async def recording_register(*args, **kw):
            nonlocal registrations
            outcome = await real(*args, **kw)
            if outcome == "registered":
                registrations += 1
            return outcome

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if registrations >= 2:
                raise asyncio.CancelledError

        with patch(
            "aiohttp.ClientSession", _session_returning([200])
        ), patch.object(runtime_api, "register_with_broker", recording_register):
            with pytest.raises(asyncio.CancelledError):
                await runtime_api.registration_loop(
                    runtime_file, mind_name="example", mind_id="mind-1",
                    log=logging.getLogger("test"), sleep=fake_sleep,
                )

        assert registrations == 2
        assert sleeps[0] == 300.0
