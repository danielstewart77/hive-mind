"""Voice input: a mic button per tile, whisper on the other end.

One test per numbered requirement. The browser half runs against a real
page with `getUserMedia` and `MediaRecorder` replaced — the point is that
our state machine handles a rejection and releases the device, not that
headless Chromium owns a microphone.

Two things these tests are deliberately strict about, because the loose
version of each passes against a broken implementation:

* The upload is inspected. A tile that records nothing, posts nothing and
  types back whatever the stubbed route returned satisfies every assertion
  about the button and the pane.
* The wrong-tile bug is routing through `focusedPanel`, and tapping a
  tile's mic focuses that tile — so a two-tile test only bites if focus
  moves to the other tile while the transcript is in flight.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import auth  # noqa: E402
import main as main_mod  # noqa: E402

SESSIONS = [
    {
        "id": "sess-a",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-a",
    },
    {
        "id": "sess-b",
        "mind_id": "mind-uuid-2",
        "mind_name": "mordecai",
        "status": "running",
        "short_id": "sess-b",
    },
]

# Replaces the browser's capture stack. Records what was asked of it so a
# test can tell "started the recorder" from "only toggled a class".
MIC_STUB = """
window.__mic = {calls: [], tracksStopped: 0, denied: false, startThrows: false,
                blobBytes: [7, 8, 9]};
const md = navigator.mediaDevices || {};
Object.defineProperty(navigator, 'mediaDevices', {configurable: true, value: md});
Object.defineProperty(md, 'getUserMedia', {
    configurable: true,
    value: function () {
        if (window.__mic.denied) {
            return Promise.reject(new DOMException('denied', 'NotAllowedError'));
        }
        const track = {
            kind: 'audio', readyState: 'live',
            stop: function () { this.readyState = 'ended'; window.__mic.tracksStopped++; },
        };
        window.__mic.calls.push('getUserMedia');
        return Promise.resolve({getTracks: function () { return [track]; }});
    },
});
function FakeRecorder(stream, opts) {
    this.stream = stream;
    this.state = 'inactive';
    this.mimeType = (opts && opts.mimeType) || 'audio/webm';
}
FakeRecorder.isTypeSupported = function () { return true; };
FakeRecorder.prototype.start = function () {
    // The device can vanish between the permission grant and the recorder
    // claiming it; start() is where that surfaces.
    if (window.__mic.startThrows) { throw new DOMException('cannot start', 'UnknownError'); }
    this.state = 'recording';
    window.__mic.calls.push('start');
};
FakeRecorder.prototype.stop = function () {
    if (this.state !== 'recording') { throw new DOMException('bad state', 'InvalidStateError'); }
    this.state = 'inactive';
    window.__mic.calls.push('stop');
    const bytes = new Uint8Array(window.__mic.blobBytes);
    if (this.ondataavailable) this.ondataavailable({data: new Blob([bytes], {type: this.mimeType})});
    if (this.onstop) this.onstop();
};
window.MediaRecorder = FakeRecorder;
"""

# Only for the unattended-recording test. Playwright's clock would do this
# globally, but fast-forwarding two minutes also trips the tile's own
# staleness poll, which drops and reattaches the socket — the transcript
# then lands mid-reattach and is correctly refused. Shorten the long timer
# instead and leave the rest of the page in real time.
# Scaled, not clamped: a clamp makes every cap from one minute upwards fire
# at the same moment, so a ten-minute recording limit is indistinguishable
# from a two-minute one. Dividing keeps the ratio, and the test can assert a
# window rather than a floor.
TIMER_SCALE = 200
SHORTEN_LONG_TIMERS = """
const realSetTimeout = window.setTimeout.bind(window);
window.setTimeout = function (fn, ms) {
    return realSetTimeout(fn, (typeof ms === 'number' && ms >= 60000) ? ms / %d : ms);
};
""" % TIMER_SCALE

# The transcribing state exists only while the POST is in flight, and a
# blocking route handler stalls the greenlet that would observe it. Record
# the transitions in the page instead and read the log back afterwards.
#
# The class name alone proves nothing about what the user sees: blanking
# every state rule in the stylesheet leaves the classes intact and the button
# pixel-identical in all four states. Record the painted appearance alongside
# the class so a state that looks like every other state is a failure.
WATCH_STATES = """
window.__micStates = [];
const btn = document.querySelector('.term-panel-mic');
window.__micSig = function () {
    const cs = getComputedStyle(btn);
    return [cs.color, cs.backgroundColor, cs.borderColor, cs.boxShadow].join('|');
};
window.__micIdleSig = window.__micSig();
new MutationObserver(function () {
    window.__micStates.push({cls: btn.className, sig: window.__micSig()});
}).observe(btn, {attributes: true, attributeFilter: ['class']});
"""


def _stub_page(page, *, sessions=SESSIONS[:1]):
    page.add_init_script(MIC_STUB)
    page.route(
        "**/api/terminal/sessions*",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(sessions)
        ),
    )
    page.route(
        "**/api/terminal/labels*",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )


def _open_tiles(page, base, count=1, routes=None):
    """Mount `count` tiles; hand back what each one's socket received."""
    sent = {}

    def handle_ws(ws):
        key = ws.url.split("/attach/")[1].split("?")[0]
        sent.setdefault(key, [])
        if routes is not None:
            routes.append(ws)
        ws.on_message(
            lambda m: sent[key].append(m if isinstance(m, str) else m.decode("utf8", "replace"))
        )
        ws.send(b"ready\r\n")

    page.route_web_socket("**/api/terminal/attach/**", handle_ws)
    page.goto(base + "/terminal")
    page.wait_for_selector(".term-card", timeout=15000)
    for i in range(count):
        # Re-query every time: opening a tile re-renders the rail, which
        # stales any handle taken before it.
        page.query_selector_all(".term-card")[i].click()
        page.wait_for_timeout(500)
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(600)
    return sent


def _typed(sent, key="sess-a"):
    """Keystroke bytes only — the socket also carries resize control frames."""
    out = []
    for frame in sent.get(key, []):
        try:
            if isinstance(json.loads(frame), dict):
                continue
        except (ValueError, TypeError):
            pass
        out.append(frame)
    return "".join(out)


def _stt_route(page, *, body=None, status=200, delay_ms=0, capture=None):
    def handler(route):
        if capture is not None:
            capture.append(route.request.post_data_buffer)
        if delay_ms:
            page.wait_for_timeout(delay_ms)
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body if body is not None else {"text": "hello there"}),
        )

    page.route("**/api/terminal/stt", handler)


def _tap(page, selector=".term-panel-mic"):
    page.click(selector)
    page.wait_for_timeout(250)


def _assert_error_shown(page):
    """The error class earns its keep only if it repaints the button.

    Requires WATCH_STATES to have been evaluated while the button was idle.
    """
    assert "is-error" in page.get_attribute(".term-panel-mic", "class")
    assert page.evaluate("() => window.__micSig()") != page.evaluate(
        "() => window.__micIdleSig"
    ), "the error state is pixel-identical to idle"


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


# 1. A tile's header renders a mic button next to the speaker button.
def test_the_tile_header_has_a_mic_button_beside_the_speaker(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)

    placement = page.evaluate(
        """() => {
            const mic = document.querySelector('.term-panel-mic');
            if (!mic) return null;
            const box = mic.getBoundingClientRect();
            return {
                inHead: !!mic.closest('.term-panel-head'),
                visible: box.width > 0 && box.height > 0,
                nextIsSpeaker: !!(mic.nextElementSibling &&
                    mic.nextElementSibling.classList.contains('term-panel-speaker')),
            };
        }"""
    )
    assert placement == {"inHead": True, "visible": True, "nextIsSpeaker": True}


# 2. Tapping begins capture, tapping again ends it, and a third begins a new one.
def test_taps_start_then_stop_then_start_the_recorder(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)
    _stt_route(page)

    _tap(page)
    _tap(page)
    page.wait_for_timeout(400)
    _tap(page)

    calls = [c for c in page.evaluate("() => window.__mic.calls") if c != "getUserMedia"]
    assert calls == ["start", "stop", "start"]


# 3. While capture is running the button reads as recording.
def test_the_button_reads_as_recording_while_capturing(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)
    _stt_route(page)
    page.evaluate(WATCH_STATES)   # records what idle looks like

    _tap(page)

    assert page.get_attribute(".term-panel-mic", "class").find("is-recording") >= 0
    assert page.get_attribute(".term-panel-mic", "aria-pressed") == "true"
    # …and it looks different, on a tile carrying a colour label whose
    # inline text colour is written over every button in the header.
    assert page.evaluate("() => window.__micSig()") != page.evaluate(
        "() => window.__micIdleSig"
    ), "recording is pixel-identical to idle"


# 4. Between the second tap and the transcript, the button reads as working,
#    and that is a different state from recording.
def test_the_button_reads_as_working_while_transcribing(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)
    _stt_route(page, delay_ms=700)

    page.evaluate(WATCH_STATES)
    _tap(page)
    page.click(".term-panel-mic")
    page.wait_for_timeout(1600)

    states = page.evaluate("() => window.__micStates")
    idle_sig = page.evaluate("() => window.__micIdleSig")
    working = [s for s in states if "is-working" in s["cls"]]
    recording = [s for s in states if "is-recording" in s["cls"]]
    assert working, f"never entered a working state: {states}"
    assert recording, f"never entered a recording state: {states}"
    # Simultaneously recording and working is two states, not one.
    assert all("is-recording" not in s["cls"] for s in working)
    # And distinct on screen, not merely in the class list.
    assert all(s["sig"] != idle_sig for s in working), "working looks like idle"
    assert not ({s["sig"] for s in working} & {s["sig"] for s in recording}), (
        "working looks like recording"
    )


# 5. A successful transcription sends the spoken text to the terminal with
#    no carriage return after it.
def test_the_transcript_is_typed_into_the_terminal_without_a_return(page):
    page, base = page
    _stub_page(page)
    sent = _open_tiles(page, base)
    uploads = []
    _stt_route(page, body={"text": "run the tests"}, capture=uploads)

    _tap(page)
    _tap(page)
    page.wait_for_timeout(800)

    assert _typed(sent).endswith("run the tests")
    assert "\r" not in _typed(sent)
    # The recorded audio is what was uploaded — not an empty body with the
    # answer supplied by the stub.
    assert uploads and bytes([7, 8, 9]) in uploads[0]


# 6. When the microphone is unavailable or refused, the button goes to the
#    error state and nothing is sent to the terminal.
def test_a_refused_microphone_shows_the_error_state_and_types_nothing(page):
    page, base = page
    _stub_page(page)
    sent = _open_tiles(page, base)
    _stt_route(page)
    page.evaluate(WATCH_STATES)
    page.evaluate("() => { window.__mic.denied = true; }")
    before = _typed(sent)

    _tap(page)
    page.wait_for_timeout(400)

    _assert_error_shown(page)
    assert _typed(sent) == before


# 7. When the transcription request fails, the button goes to the error
#    state and nothing is sent to the terminal.
def test_a_failed_transcription_shows_the_error_state_and_types_nothing(page):
    page, base = page
    _stub_page(page)
    sent = _open_tiles(page, base)
    _stt_route(page, status=502, body={"detail": "voice server unavailable"})
    page.evaluate(WATCH_STATES)
    before = _typed(sent)

    _tap(page)
    _tap(page)
    page.wait_for_timeout(800)

    _assert_error_shown(page)
    assert _typed(sent) == before


# 7b. Unreachable includes "never answers". Without a deadline the button
#     sits in the one state the click handler will not let the user leave.
def test_a_transcription_that_never_answers_shows_the_error_state(page):
    page, base = page
    page.add_init_script(SHORTEN_LONG_TIMERS)   # scales the round-trip cap too
    _stub_page(page)
    sent = _open_tiles(page, base)
    # Routed, and never fulfilled.
    page.route("**/api/terminal/stt", lambda route: None)
    page.evaluate(WATCH_STATES)
    before = _typed(sent)

    _tap(page)
    _tap(page)
    page.wait_for_timeout(1500)

    _assert_error_shown(page)
    assert _typed(sent) == before


# 8. When the transcript comes back empty, the button goes to the error
#    state and nothing is sent to the terminal.
def test_an_empty_transcript_shows_the_error_state_and_types_nothing(page):
    page, base = page
    _stub_page(page)
    sent = _open_tiles(page, base)
    _stt_route(page, body={"text": "   "})
    page.evaluate(WATCH_STATES)
    before = _typed(sent)

    _tap(page)
    _tap(page)
    page.wait_for_timeout(800)

    _assert_error_shown(page)
    assert _typed(sent) == before


# 9. A transcription request carrying no valid login is refused.
def test_transcription_without_a_login_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.post(
        "/api/terminal/stt", files={"file": ("audio.webm", b"1234", "audio/webm")}
    )

    # Not merely "an error": an unguarded route reaches the voice server and
    # answers 502, which any >=400 assertion would have accepted.
    assert response.status_code == 401


# 10. Speech captured in one tile is sent to that tile's socket and to no
#     other open tile's.
def test_the_transcript_goes_to_the_recording_tile_not_the_focused_one(page):
    page, base = page
    _stub_page(page, sessions=SESSIONS)
    sent = _open_tiles(page, base, count=2)
    _stt_route(page, body={"text": "for the first tile"}, delay_ms=700)

    first = page.query_selector_all(".term-panel")[0]
    first.query_selector(".term-panel-mic").click()
    page.wait_for_timeout(250)
    first.query_selector(".term-panel-mic").click()
    # Focus the other tile while the transcript is still in flight — routing
    # through `focusedPanel` is indistinguishable from correct without this.
    page.query_selector_all(".term-panel")[1].click()
    page.wait_for_timeout(1600)

    assert "for the first tile" in _typed(sent, "sess-a")
    assert "for the first tile" not in _typed(sent, "sess-b")


# 11. At phone width the mic button is not displayed.
def test_the_mic_is_not_shown_at_phone_width(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(
            viewport={"width": 412, "height": 780}, is_mobile=True, has_touch=True
        )
        ctx.add_cookies([{
            "name": auth.SESSION_COOKIE_NAME, "value": token,
            "domain": "127.0.0.1", "path": "/",
        }])
        page = ctx.new_page()
        _stub_page(page)
        _open_tiles(page, base)

        # Not shown — however that is achieved. Demanding the element still
        # exist would mandate one implementation and reject a panel that
        # simply never renders the button at this width.
        assert not page.is_visible(".term-panel-mic")

        # The breakpoint is where it is claimed to be. A stylesheet hiding
        # the button below 500px would satisfy a single phone-width probe.
        page.set_viewport_size({"width": 859, "height": 780})
        page.wait_for_timeout(200)
        assert not page.is_visible(".term-panel-mic")

        # Turned sideways, this is still a phone: 915px, wider than the
        # breakpoint, and still dictating into its own keyboard. It is also
        # where the compose box appears, and the mic would type its transcript
        # straight past that box to the pty — so width alone stopped being the
        # question. Every other test in this file runs in the desktop `page`
        # fixture and clicks the button, which is where it does still show.
        page.set_viewport_size({"width": 915, "height": 412})
        page.wait_for_timeout(200)
        assert not page.is_visible(".term-panel-mic")
        browser.close()


# 12. After the transcript is typed, the keyboard returns to the terminal.
def test_the_terminal_takes_the_keyboard_back_after_a_transcript(page):
    page, base = page
    _stub_page(page)
    sent = _open_tiles(page, base)
    _stt_route(page, body={"text": "ready"})

    _tap(page)
    _tap(page)
    page.wait_for_timeout(800)
    # Enter would re-arm a still-focused mic button instead of submitting.
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)

    assert _typed(sent).endswith("\r")
    assert page.evaluate(
        "() => document.activeElement.classList.contains('term-panel-mic')"
    ) is False


# 12b. …but only from the mic button. A transcript landing after the user
#      has clicked into another tile must not pull the keyboard back, or the
#      rest of their sentence goes to the wrong pty.
def test_a_late_transcript_does_not_steal_the_keyboard_from_another_tile(page):
    page, base = page
    _stub_page(page, sessions=SESSIONS)
    sent = _open_tiles(page, base, count=2)
    _stt_route(page, body={"text": "for the first tile"}, delay_ms=900)

    panels = page.query_selector_all(".term-panel")
    panels[0].query_selector(".term-panel-mic").click()
    page.wait_for_timeout(250)
    panels[0].query_selector(".term-panel-mic").click()
    # Into the other tile while the transcript is still in flight.
    panels[1].query_selector(".xterm-screen").click()
    page.wait_for_timeout(1400)
    page.keyboard.type("mine")
    page.wait_for_timeout(300)

    assert "for the first tile" in _typed(sent, "sess-a")
    assert "mine" in _typed(sent, "sess-b"), "the transcript stole the keyboard"
    assert "mine" not in _typed(sent, "sess-a")


# 13. A transcript that cannot be delivered shows the error state rather
#     than being silently dropped.
def test_a_transcript_with_no_live_terminal_shows_the_error_state(page):
    page, base = page
    _stub_page(page)
    routes = []
    _open_tiles(page, base, routes=routes)
    _stt_route(page, body={"text": "into the void"}, delay_ms=900)
    page.evaluate(WATCH_STATES)

    _tap(page)
    _tap(page)
    # The socket drops while the transcript is in flight. 1012 is the
    # gateway's "another window took the keyboard" — the tile stands down
    # for good rather than reconnecting, so this is the durable case where
    # sendRaw would discard the bytes and report nothing.
    routes[0].close(code=1012)
    page.wait_for_timeout(1800)

    # Read the transition log, not the live class: the error is a two-second
    # flash, and on a loaded machine the assertion lands after it has gone.
    states = page.evaluate("() => window.__micStates")
    errored = [s for s in states if "is-error" in s["cls"]]
    assert errored, f"the undeliverable transcript reported nothing: {states}"
    idle_sig = page.evaluate("() => window.__micIdleSig")
    assert all(s["sig"] != idle_sig for s in errored), "the error state is invisible"


# 14. Tapping the mic while it is transcribing does nothing.
def test_a_tap_while_transcribing_does_not_start_another_recording(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)
    _stt_route(page, delay_ms=900)

    _tap(page)
    page.click(".term-panel-mic")
    page.wait_for_timeout(250)
    page.click(".term-panel-mic")  # lands in the transcribing window
    page.wait_for_timeout(1400)

    calls = [c for c in page.evaluate("() => window.__mic.calls") if c != "getUserMedia"]
    assert calls == ["start", "stop"]


# 14b. Narrowing past the breakpoint takes the button away mid-recording. The
#      capture it abandons must not come back: stopping a recorder is not
#      cancelling it, and a transcript that still lands leaves the state
#      machine idle with words in flight — a second tap then races them.
def test_a_recording_abandoned_by_a_narrowing_window_does_not_come_back(page):
    page, base = page
    _stub_page(page)
    sent = _open_tiles(page, base)
    _stt_route(page, body={"text": "never asked for"}, delay_ms=400)
    before = _typed(sent)

    _tap(page)
    page.set_viewport_size({"width": 500, "height": 780})
    page.wait_for_timeout(1200)

    assert _typed(sent) == before, "an abandoned recording still typed its words"
    assert page.evaluate("() => window.__mic.tracksStopped") == 1


# 15. The microphone is released when recording ends and when the tile goes.
def test_the_microphone_is_released_after_a_recording_and_on_teardown(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)
    _stt_route(page)

    _tap(page)
    _tap(page)
    page.wait_for_timeout(800)
    assert page.evaluate("() => window.__mic.tracksStopped") == 1

    # A tile closed mid-recording must not leave the device held.
    _tap(page)
    page.click(".term-panel-close")
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__mic.tracksStopped") == 2


# 15b. Suspended from elsewhere, and refused by the recorder: two ways to
#      end up holding a live microphone with nothing on screen saying so.
def test_the_microphone_is_released_when_the_session_stands_down(page):
    page, base = page
    _stub_page(page)
    routes = []
    _open_tiles(page, base, routes=routes)
    _stt_route(page)

    _tap(page)
    # 4411 is the gateway suspending the session from another surface. The
    # tile survives; the recording it can no longer deliver must not.
    routes[0].close(code=4411)
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__mic.tracksStopped") == 1
    assert "is-recording" not in page.get_attribute(".term-panel-mic", "class")


# 15c. The recorder can throw at start() — the device is granted, then lost.
#      An unwrapped throw there holds the microphone with the button showing
#      nothing at all.
def test_a_recorder_that_refuses_to_start_gives_the_device_back(page):
    page, base = page
    _stub_page(page)
    _open_tiles(page, base)
    _stt_route(page)
    page.evaluate(WATCH_STATES)
    page.evaluate("() => { window.__mic.startThrows = true; }")

    _tap(page)
    page.wait_for_timeout(400)

    assert page.evaluate("() => window.__mic.tracksStopped") == 1
    _assert_error_shown(page)


# 16. A recording that is never stopped ends itself after two minutes.
def test_a_recording_left_running_stops_itself(page):
    page, base = page
    page.add_init_script(SHORTEN_LONG_TIMERS)
    _stub_page(page)
    sent = _open_tiles(page, base)
    _stt_route(page, body={"text": "left running"})

    # Not _tap: its own settling wait would eat into the window being timed.
    page.click(".term-panel-mic")   # started, and never stopped by hand

    # Still going at half the cap. A limit of a minute or less would already
    # have fired here.
    page.wait_for_timeout(120000 / TIMER_SCALE / 2)
    running = [c for c in page.evaluate("() => window.__mic.calls") if c != "getUserMedia"]
    assert running == ["start"], f"stopped before the cap: {running}"

    # …and over by three times it. A ten-minute cap would not have fired.
    page.wait_for_timeout(120000 / TIMER_SCALE * 2.5)
    calls = [c for c in page.evaluate("() => window.__mic.calls") if c != "getUserMedia"]
    assert calls == ["start", "stop"]
    assert "left running" in _typed(sent)


# 17. The words typed are the words the voice server returned, and the audio
#     recorded is the audio sent to it.
def test_the_proxy_relays_the_audio_and_returns_the_voice_servers_words(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_API_URL", "http://hive-mind-voice:8422")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"text": "the quick brown fox"}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["ctype"] = req.headers.get("Content-type", "")
        return _FakeResp()

    with patch("main.urllib.request.urlopen", side_effect=_fake_urlopen):
        response = client.post(
            "/api/terminal/stt",
            files={"file": ("audio.webm", b"RECORDED-AUDIO", "audio/webm")},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "the quick brown fox"
    assert captured["url"].endswith("/stt")
    assert "multipart/form-data" in captured["ctype"]
    assert b"RECORDED-AUDIO" in captured["body"]
    assert b"audio/webm" in captured["body"]


# 17b. …and nothing else. Whatever comes back is typed straight into a pty,
#      so a body that is not the shape promised must not become a 500, and a
#      control character must not reach the pane.
@pytest.mark.parametrize(
    "upstream,expect_status,expect_text",
    [
        ({"text": "one  two\n three"}, 200, "one two three"),
        ({"text": "rm -rf \x1b[200~/\x08\x7f"}, 200, "rm -rf [200~/"),
        ({"text": None}, 200, ""),
        ({"text": ["a", "b"]}, 200, ""),
        ("just a string", 502, None),
        ([1, 2, 3], 502, None),
        (None, 502, None),
    ],
)
def test_the_proxy_refuses_a_body_it_cannot_speak_for(
    tmp_path, monkeypatch, upstream, expect_status, expect_text
):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_API_URL", "http://hive-mind-voice:8422")

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(upstream).encode()

    with patch("main.urllib.request.urlopen", side_effect=lambda *a, **k: _FakeResp()):
        response = client.post(
            "/api/terminal/stt", files={"file": ("audio.webm", b"AUDIO", "audio/webm")}
        )

    assert response.status_code == expect_status
    if expect_text is not None:
        assert response.json()["text"] == expect_text


def test_an_oversized_upload_is_refused_before_the_voice_server_sees_it(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_API_URL", "http://hive-mind-voice:8422")
    reached = []

    with patch(
        "main.urllib.request.urlopen", side_effect=lambda *a, **k: reached.append(1)
    ):
        response = client.post(
            "/api/terminal/stt",
            files={
                "file": (
                    "audio.webm",
                    b"x" * (main_mod.MAX_STT_AUDIO_BYTES + 1),
                    "audio/webm",
                )
            },
        )

    assert response.status_code == 413
    assert not reached
