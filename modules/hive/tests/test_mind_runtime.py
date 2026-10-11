"""The console configures every Mind through its HTTP API, not a bind mount.

One code path for a container in this stack, a bare-metal Mind on this host,
and a Mind on another machine — the last of which no mount can reach.
"""

import asyncio
import os
import sys

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mind_runtime


class _Client:
    """Stand-in for httpx.AsyncClient that answers with a fixed response."""

    def __init__(self, response=None, error=None, calls=None):
        self._response = response
        self._error = error
        self.calls = calls if calls is not None else []

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response

    async def get(self, url, **kwargs):
        return await self._respond("GET", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._respond("PATCH", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._respond("PUT", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._respond("POST", url, **kwargs)


def _response(status: int, json_body=None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_body if json_body is not None else None,
        text=None if json_body is not None else text,
        request=httpx.Request("GET", "http://mind/runtime"),
    )


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")


def _run(coro):
    return asyncio.run(coro)


def test_fetch_reads_the_minds_runtime_route(monkeypatch):
    async def scenario():
        client = _Client(_response(200, {"configuration": {"default_model": "opus"}}))
        monkeypatch.setattr(httpx, "AsyncClient", client)

        configuration = await mind_runtime.fetch_configuration("http://bob:8420/")

        assert configuration["default_model"] == "opus"
        assert client.calls[0][1] == "http://bob:8420/runtime"

    _run(scenario())


def test_push_sends_the_admin_bearer(monkeypatch):
    async def scenario():
        client = _Client(_response(200, {"configuration": {"default_model": "opus"}}))
        monkeypatch.setattr(httpx, "AsyncClient", client)

        await mind_runtime.push_default_model("http://bob:8420", "opus")

        method, url, kwargs = client.calls[0]
        assert (method, url) == ("PATCH", "http://bob:8420/runtime")
        assert kwargs["json"] == {"default_model": "opus"}
        assert kwargs["headers"]["Authorization"] == "Bearer admin-token"

    _run(scenario())


def test_a_mind_that_is_down_is_a_bad_gateway(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(error=httpx.ConnectError("refused"))
        )

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.fetch_configuration("http://bob:8420")

        assert exc.value.status_code == 502

    _run(scenario())


def test_a_mind_without_the_route_says_so(monkeypatch):
    async def scenario():
        monkeypatch.setattr(httpx, "AsyncClient", _Client(_response(404, text="nope")))

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.fetch_configuration("http://bob:8420")

        assert exc.value.status_code == 501

    _run(scenario())


def test_a_rejected_write_keeps_its_status(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(_response(401, text="unauthorized"))
        )

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.push_default_model("http://bob:8420", "opus")

        assert exc.value.status_code == 401

    _run(scenario())


def test_a_mind_with_no_gateway_url_is_a_bad_request():
    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await mind_runtime.fetch_configuration("")

        assert exc.value.status_code == 400

    _run(scenario())


def test_no_admin_token_refuses_the_write(monkeypatch):
    async def scenario():
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(_response(200, {"configuration": {}}))
        )

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.push_default_model("http://bob:8420", "opus")

        assert exc.value.status_code == 503

    _run(scenario())

# ---------------------------------------------------------------- model picker


CATALOG = [
    {"name": "claude-opus-5", "label": "Opus 5", "provider": "anthropic",
     "provider_label": "Anthropic", "context_window": 1_000_000,
     "rotation_threshold_percent": 30},
    {"name": "qwen35-131k", "label": "Qwen 3.5 9B (local, 131k)",
     "provider": "ollama", "provider_label": "Ollama"},
]


def _picker(monkeypatch, *, harness, provider):
    """The rows this Mind's editor would offer."""
    import main as main_mod

    async def fake_configuration(_gateway):
        return {"name": "m", "harness": harness, "provider": provider,
                "default_model": "x", "deployment": "container"}

    async def fake_gateway_url(_name):
        return "http://m:8420"

    async def fake_models(_gateway):
        return CATALOG

    monkeypatch.setattr(main_mod.mind_runtime, "fetch_configuration", fake_configuration)
    monkeypatch.setattr(main_mod, "_mind_gateway_url", fake_gateway_url)
    monkeypatch.setattr(main_mod.mind_runtime, "fetch_models", fake_models)
    return asyncio.run(main_mod.api_mind_configuration("m", user={"sub": "t"}))["models"]


def test_the_console_relays_what_the_mind_reported_without_re_sorting_it(monkeypatch):
    """The Mind's own key and harness already decided the list.

    The console used to bucket the listing itself, by a `provider` field that
    means something different on each side — a codex Mind declares `anthropic`
    whatever it runs — and every Mind it guessed wrong about got an empty
    picker. Grouping now follows the provider each row names.
    """
    rows = _picker(monkeypatch, harness="codex_cli", provider="anthropic")
    assert [row["name"] for row in rows] == ["claude-opus-5", "qwen35-131k"]


def test_each_offered_row_carries_its_label_and_provider(monkeypatch):
    """A picker shows the label, saves the name, groups by the provider."""
    rows = {row["name"]: row for row in _picker(
        monkeypatch, harness="claude_cli", provider="ollama"
    )}
    assert rows["claude-opus-5"]["label"] == "Opus 5"
    assert rows["qwen35-131k"]["provider"] == "ollama"
    assert rows["qwen35-131k"]["provider_label"] == "Ollama"


def test_each_offered_row_carries_its_models_rotation_percent_and_window(monkeypatch):
    """The rotation threshold is the model's, so the picker shows it per row."""
    rows = {row["name"]: row for row in _picker(
        monkeypatch, harness="claude_cli", provider="anthropic"
    )}
    assert (rows["claude-opus-5"]["rotation_threshold_percent"],
            rows["claude-opus-5"]["context_window"]) == (30, 1_000_000)
    assert rows["qwen35-131k"]["rotation_threshold_percent"] is None


# ---------------------------------------------------------------------------
# The voice
# ---------------------------------------------------------------------------
def test_saving_a_voice_patches_the_mind_it_names(monkeypatch):
    """The voice is written on the Mind, over its own runtime route.

    A console that wrote a bind-mounted copy would reach a container in this
    stack and nothing else, and the Mind re-registers from its own file on
    every boot — so a write anywhere but the file is undone by a restart.
    """

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_response(200, {"configuration": {"voice": "bm_george"}}), calls=calls),
        )

        configuration = await mind_runtime.push_voice("http://cypher:8420", "bm_george")

        assert configuration["voice"] == "bm_george"
        method, url, kwargs = calls[0]
        assert (method, url) == ("PATCH", "http://cypher:8420/runtime")
        assert kwargs["json"] == {"voice": "bm_george"}
        assert kwargs["headers"]["Authorization"] == "Bearer admin-token"

    _run(scenario())


def test_saving_a_voice_sends_only_the_voice(monkeypatch):
    """Resending the model the page rendered would write a stale one back."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_response(200, {"configuration": {"voice": "af_heart"}}), calls=calls),
        )

        await mind_runtime.push_voice("http://cypher:8420", "af_heart")

        assert "default_model" not in calls[0][2]["json"]

    _run(scenario())


def test_saving_a_voice_and_its_engine_travel_in_one_request(monkeypatch):
    """Test 20: what a voice means depends on the engine beside it.

    Two requests would leave the Mind briefly holding one engine's selection
    under the other, and whichever one failed would leave it there.
    """

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(
                _response(
                    200,
                    {
                        "configuration": {
                            "voice": "_dramitac_mono_voice_ref.wav",
                            "voice_engine": "chatterbox",
                        }
                    },
                ),
                calls=calls,
            ),
        )

        configuration = await mind_runtime.push_voice(
            "http://cypher:8420", "_dramitac_mono_voice_ref.wav", "chatterbox"
        )

        assert configuration["voice_engine"] == "chatterbox"
        assert len(calls) == 1
        assert calls[0][2]["json"] == {
            "voice": "_dramitac_mono_voice_ref.wav",
            "voice_engine": "chatterbox",
        }

    _run(scenario())


def test_saving_a_voice_without_an_engine_leaves_the_engine_alone(monkeypatch):
    """A Kokoro picker on a Mind already on Kokoro restates nothing."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_response(200, {"configuration": {"voice": "af_heart"}}), calls=calls),
        )

        await mind_runtime.push_voice("http://cypher:8420", "af_heart")

        assert "voice_engine" not in calls[0][2]["json"]

    _run(scenario())


def test_a_chatterbox_sample_names_the_mind_whose_clip_it_is(monkeypatch):
    """The recording lives in one Mind's directory; the server must be told."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_audio_response(200, b"OggS"), calls=calls),
        )

        await mind_runtime.sample_voice(
            "http://voice:8422", "_dramitac_mono_voice_ref.wav", "skippy"
        )

        assert calls[0][2]["json"] == {
            "voice": "_dramitac_mono_voice_ref.wav",
            "voice_id": "skippy",
        }

    _run(scenario())


def test_a_kokoro_sample_names_no_mind(monkeypatch):
    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_audio_response(200, b"OggS"), calls=calls),
        )

        await mind_runtime.sample_voice("http://voice:8422", "bm_george")

        assert calls[0][2]["json"] == {"voice": "bm_george"}

    _run(scenario())


def test_a_mind_with_no_gateway_url_refuses_rather_than_guessing():
    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await mind_runtime.push_voice("", "af_heart")

        assert exc.value.status_code == 400

    _run(scenario())


def test_an_unreachable_voice_server_reports_unreadable_not_empty(monkeypatch):
    """Empty would say this engine has no voices, which is never true."""

    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(error=httpx.ConnectError("refused"))
        )

        body = await mind_runtime.fetch_voice_catalogue("http://voice:8422")

        assert body["catalogue"]["readable"] is False
        assert body["catalogue"]["voices"] == []
        assert "could not be reached" in body["catalogue"]["detail"]

    _run(scenario())


def test_the_catalogue_is_relayed_as_the_voice_server_reported_it(monkeypatch):
    async def scenario():
        offered = {
            "engine": "kokoro",
            "default_voice": "af_heart",
            "catalogue": {
                "language_code": "b",
                "readable": True,
                "detail": "",
                "voices": [
                    {"name": "bm_george", "language": "British English",
                     "gender": "male", "label": "George — British English, male"}
                ],
            },
        }
        monkeypatch.setattr(httpx, "AsyncClient", _Client(_response(200, offered)))

        body = await mind_runtime.fetch_voice_catalogue("http://voice:8422")

        assert body["catalogue"]["voices"][0]["label"] == "George — British English, male"
        assert body["default_voice"] == "af_heart"

    _run(scenario())


def _audio_response(status: int, body: bytes = b"") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=body,
        request=httpx.Request("POST", "http://voice:8422/voices/sample"),
    )


def test_a_sample_asks_the_voice_server_for_the_voice_it_names(monkeypatch):
    """The Listen button. A letter grade is not what a voice sounds like."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_audio_response(200, b"OggS-sample"), calls=calls),
        )

        audio = await mind_runtime.sample_voice("http://voice:8422/", "bm_george")

        assert audio == b"OggS-sample"
        method, url, kwargs = calls[0]
        assert (method, url) == ("POST", "http://voice:8422/voices/sample")
        assert kwargs["json"] == {"voice": "bm_george"}

    _run(scenario())


def test_a_sample_with_no_voice_named_is_refused(monkeypatch):
    """Sampling "whatever the default is" answers a question nobody asked."""

    async def scenario():
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.sample_voice("http://voice:8422", "  ")

        assert refused.value.status_code == 400

    _run(scenario())


def test_a_sample_with_no_voice_server_configured_says_so(monkeypatch):
    """Not "unreachable": nothing was reached for, and the remedy differs."""

    async def scenario():
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.sample_voice("", "bm_george")

        assert refused.value.status_code == 502
        assert "No voice server is configured" in refused.value.detail

    _run(scenario())


def test_the_voice_servers_own_refusal_is_carried_through(monkeypatch):
    """It knows which voices it offers; this process does not."""

    async def scenario():
        monkeypatch.setattr(httpx, "AsyncClient", _Client(_audio_response(400)))

        with pytest.raises(HTTPException) as refused:
            await mind_runtime.sample_voice("http://voice:8422", "zf_xiaobei")

        assert refused.value.status_code == 400
        assert "zf_xiaobei" in refused.value.detail

    _run(scenario())


def test_an_unreachable_voice_server_reports_a_gateway_failure(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(error=httpx.ConnectError("refused"))
        )

        with pytest.raises(HTTPException) as refused:
            await mind_runtime.sample_voice("http://voice:8422", "bm_george")

        assert refused.value.status_code == 502
        assert "unreachable" in refused.value.detail

    _run(scenario())


def test_a_voice_server_that_broke_reads_as_a_gateway_failure(monkeypatch):
    """Its 500 is not the console's 500, and not the operator's fault either."""

    async def scenario():
        monkeypatch.setattr(httpx, "AsyncClient", _Client(_audio_response(500)))

        with pytest.raises(HTTPException) as refused:
            await mind_runtime.sample_voice("http://voice:8422", "bm_george")

        assert refused.value.status_code == 502

    _run(scenario())


# ---------------------------------------------------------------------------
# The settings panel
# ---------------------------------------------------------------------------

_SETTINGS_BODY = {
    "harness": "dsh_cli",
    "context_window": 131072,
    "settings": [
        {
            "key": "goal_rounds",
            "label": "Goal rounds",
            "kind": "number",
            "help": "",
            "choices": [],
            "minimum": 1,
            "maximum": 200,
            "present": True,
            "value": 40,
        }
    ],
}


def test_the_settings_read_asks_the_mind_and_carries_the_admin_bearer(monkeypatch):
    async def scenario():
        calls = []
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(_response(200, _SETTINGS_BODY), calls=calls),
        )
        body = await mind_runtime.fetch_settings("http://mind:8420")
        assert body["settings"][0]["value"] == 40
        method, url, kwargs = calls[0]
        assert (method, url) == ("GET", "http://mind:8420/runtime/settings")
        assert kwargs["headers"]["Authorization"] == "Bearer admin-token"

    _run(scenario())


def test_a_save_sends_only_the_settings_that_changed(monkeypatch):
    """A form that resent every field would write a value the operator never
    touched back over an edit made elsewhere since the page was opened."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(_response(200, _SETTINGS_BODY), calls=calls),
        )
        await mind_runtime.push_settings("http://mind:8420", {"goal_rounds": 1})
        method, url, kwargs = calls[0]
        assert (method, url) == ("PATCH", "http://mind:8420/runtime/settings")
        assert kwargs["json"] == {"settings": {"goal_rounds": 1}}

    _run(scenario())


def test_a_refused_setting_is_reported_as_a_refusal_with_the_minds_reason(monkeypatch):
    """Painting the panel green over a refusal is how an operator believes a
    value landed that never did."""

    async def scenario():
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(_response(400, {"error": "Goal rounds must be a whole number"})),
        )
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.push_settings("http://mind:8420", {"goal_rounds": "x"})
        assert refused.value.status_code == 400
        # The sentence itself, not a substring of the raw body: the raw JSON
        # also contains those words, so a looser assertion holds while the
        # operator is shown a serialized envelope.
        assert refused.value.detail == "Goal rounds must be a whole number"

    _run(scenario())


def test_a_mind_without_the_settings_api_is_not_reported_as_a_bad_request(monkeypatch):
    """A 404 means this Mind predates the route, and the remedy is a deploy —
    not a different value in the form."""

    async def scenario():
        monkeypatch.setattr(
            mind_runtime.httpx, "AsyncClient", _Client(_response(404, text="nope"))
        )
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.fetch_settings("http://mind:8420")
        assert refused.value.status_code == 501

    _run(scenario())


def test_an_unreachable_mind_is_reported_as_unreachable(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(error=httpx.ConnectError("refused")),
        )
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.push_settings("http://mind:8420", {"goal_rounds": 1})
        assert refused.value.status_code == 502

    _run(scenario())


def test_a_save_carries_the_admin_bearer_the_mind_demands(monkeypatch):
    """The Mind's settings route is admin-guarded, so a save that forgot the
    credential would 401 on every attempt in production with the shape of the
    request still perfectly correct."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(_response(200, _SETTINGS_BODY), calls=calls),
        )
        await mind_runtime.push_settings("http://mind:8420", {"goal_rounds": 1})
        assert calls[0][2]["headers"]["Authorization"] == "Bearer admin-token"

    _run(scenario())


def test_a_minds_own_refusal_keeps_its_status_rather_than_becoming_a_bad_value(
    monkeypatch,
):
    """A mind with no admin token configured answers 503. Flattened to 400 it
    reaches the operator as a complaint about the value they typed, and they
    go looking at the form instead of at the mind."""

    async def scenario():
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(_response(503, {"error": "no admin token configured"})),
        )
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.push_settings("http://mind:8420", {"goal_rounds": 1})
        assert refused.value.status_code == 503

    _run(scenario())


def test_a_mind_answering_something_other_than_settings_is_not_passed_through(
    monkeypatch,
):
    """A reverse proxy's own 200 page, or a mind answering a different route,
    would otherwise reach the panel as a settings payload with no settings in
    it."""

    async def scenario():
        monkeypatch.setattr(
            mind_runtime.httpx, "AsyncClient", _Client(_response(200, {"ok": True}))
        )
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.fetch_settings("http://mind:8420")
        assert refused.value.status_code == 502

    _run(scenario())


def test_a_save_with_nothing_in_it_never_reaches_the_mind(monkeypatch):
    """An empty save is a write the mind would have to refuse, and a round
    trip to be told so."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            mind_runtime.httpx,
            "AsyncClient",
            _Client(_response(200, _SETTINGS_BODY), calls=calls),
        )
        with pytest.raises(HTTPException):
            await mind_runtime.push_settings("http://mind:8420", {})
        assert calls == []

    _run(scenario())
