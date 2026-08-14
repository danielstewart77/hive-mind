"""The mobile compose box: type into a real field, submit on purpose.

A phone keyboard's autocorrect, prediction and voice typing all work by
rewriting text that is already in the field. xterm forwards that field to the
pty keystroke by keystroke, so every correction re-sends a region the pty
already has — the duplicated-sentence report. The box breaks the link: nothing
reaches the pty until an explicit submit, so there is no already-sent region
for the keyboard to rewrite.

One test per numbered requirement. Three requirements are proved in
tests/js/terminal_routing_test.mjs instead, because they are decisions about
values rather than about the page: the landscape-phone gate (req 3), and the
two paste-encoding branches (reqs 6 and 7) — the harness's stub socket cannot
be made to represent both a TUI that has bracketed paste on and one that has
not without asserting on the wire bytes twice over.

What these tests are strict about, because the loose version passes against a
broken implementation:

* The socket is inspected, not the textarea. "Nothing was sent" is the whole
  point of the box, and a bare unwired <textarea> satisfies every assertion
  about its own value.
* Delivery is never assumed from `sendRaw` returning true. A half-open socket
  reports OPEN for up to fifteen seconds, which is exactly when someone is
  composing on a phone.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import auth  # noqa: E402

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

BOX = ".term-compose-input"
SEND = ".term-compose-send"
# The key row, in markup order: Esc, Tab, Ctrl+C, PgUp, PgDn, arrows, return.
# Selected by position because the sequences themselves are raw control
# bytes, which no CSS attribute selector can name.
RETURN_KEY = "#term-mobile-toolbar button:nth-child(10)"
INTERRUPT_KEY = "#term-mobile-toolbar button:nth-child(3)"


def _stub_page(page, *, sessions=SESSIONS):
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


def _wire_sockets(page, routes=None, sent=None, greeting=b"ready\r\n", echo=True):
    """Mock every attach socket; hand back what each one received.

    `echo` models a pane that is actually listening: a real pty answers a
    submitted message with output, and that output is the tile's only evidence
    the bytes arrived. Turn it off to model the case the box exists to
    survive — a socket the browser still believes is open, in front of nothing.
    """
    sent = {} if sent is None else sent

    def handle_ws(ws):
        key = ws.url.split("/attach/")[1].split("?")[0]
        sent.setdefault(key, [])
        if routes is not None:
            routes.append(ws)

        def on_message(m):
            sent[key].append(m if isinstance(m, str) else m.decode("utf8", "replace"))
            if echo:
                ws.send(b".")

        ws.on_message(on_message)
        ws.send(greeting)

    page.route_web_socket("**/api/terminal/attach/**", handle_ws)
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


def _open_tile(page, base, index=0):
    page.goto(base + "/terminal")
    page.wait_for_selector(".term-card", timeout=15000)
    page.query_selector_all(".term-card")[index].click()
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(600)


def _touch_page(live_app, width, height):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(
            viewport={"width": width, "height": height},
            is_mobile=True, has_touch=True,
        )
        ctx.add_cookies([{
            "name": auth.SESSION_COOKIE_NAME, "value": token,
            "domain": "127.0.0.1", "path": "/",
        }])
        yield ctx.new_page(), base
        browser.close()


@pytest.fixture()
def phone(live_app):
    """A phone held upright."""
    yield from _touch_page(live_app, 412, 780)


@pytest.fixture()
def landscape_phone(live_app):
    """The same phone turned sideways — 915px, wider than the 859 breakpoint
    the rest of the tile's mobile layout keys off, and still a phone."""
    yield from _touch_page(live_app, 915, 412)


# 1. On a phone-width screen an open tile shows a compose box below the
#    terminal.
def test_the_compose_box_is_shown_at_phone_width(phone):
    page, base = phone
    _stub_page(page)
    _wire_sockets(page)
    _open_tile(page, base)

    assert page.is_visible(BOX)
    assert page.is_visible(SEND)
    # Below the terminal, not floating over it — an overlay would cover the
    # pane's own last lines, which is where the reply being answered is.
    box_top = page.evaluate(
        "() => document.querySelector('.term-compose').getBoundingClientRect().top"
    )
    pane_bottom = page.evaluate(
        "() => document.querySelector('.term-xterm').getBoundingClientRect().bottom"
    )
    assert box_top >= pane_bottom - 1


# 2. On a desktop-width screen with a mouse, no compose box is shown.
def test_no_compose_box_at_desktop_width(page):
    page, base = page
    _stub_page(page)
    _wire_sockets(page)
    _open_tile(page, base)

    # Not shown — however that is achieved. The desktop `page` fixture is a
    # 1100px context with no touch, so neither arm of the gate matches.
    assert not page.is_visible(BOX)

    # The breakpoint is where it is claimed to be. A gate tripping at 500px
    # would satisfy a single desktop-width probe.
    #
    # The gate's own state, not painted visibility: below 860 the mobile
    # shell switches to one-view-at-a-time and hides the whole stage until
    # the view is the stage, so nothing inside the tile is visible at 859
    # whatever the box does. What is being asserted is the decision.
    shown = f"() => !document.querySelector('.term-compose').hidden"
    page.set_viewport_size({"width": 859, "height": 780})
    page.wait_for_timeout(250)
    assert page.evaluate(shown) is True
    page.set_viewport_size({"width": 860, "height": 780})
    page.wait_for_timeout(250)
    assert page.evaluate(shown) is False


# 3. A phone in landscape — wide, but touch — still shows the box, and the
#    key row with it.
def test_a_landscape_phone_keeps_the_box_and_the_key_row(landscape_phone):
    page, base = landscape_phone
    _stub_page(page)
    _wire_sockets(page)
    _open_tile(page, base)

    # 915px is a desktop as far as the 859 breakpoint is concerned. Gating on
    # width alone takes the box away when the phone is turned, handing the
    # duplication straight back on the same device, mid-sentence.
    assert page.is_visible(BOX)
    # And the key row answers to the same gate. Wired separately, this is
    # where a landscape phone ends up with a compose box and no Esc, no
    # Ctrl+C, no arrows and no way to send a bare return to the pane.
    assert page.is_visible(RETURN_KEY)
    assert page.is_visible(INTERRUPT_KEY)


# 4. Typing in the box sends nothing until submit; the arrow then sends
#    exactly what was typed, once.
def test_nothing_is_sent_until_the_arrow_and_then_it_is_sent_once(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    page.fill(BOX, "the quick brown fox")
    page.wait_for_timeout(400)
    assert "quick" not in _typed(sent), "the box leaked keystrokes to the pty"

    page.click(SEND)
    page.wait_for_timeout(400)
    wire = _typed(sent)
    assert "the quick brown fox" in wire
    assert wire.count("the quick brown fox") == 1, f"sent more than once: {wire!r}"
    assert wire.endswith("\r"), f"nothing submitted the message: {wire!r}"


# 5. Return inside the box inserts a newline and sends nothing.
def test_return_in_the_box_adds_a_line_and_sends_nothing(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    page.click(BOX)
    page.keyboard.type("first line")
    page.keyboard.press("Enter")
    page.keyboard.type("second line")
    page.wait_for_timeout(400)

    assert page.input_value(BOX) == "first line\nsecond line"
    assert "first line" not in _typed(sent), "Enter submitted instead of newlining"


# 6. A multi-line message is delivered as one bracketed block with a single
#    trailing return when the pane has bracketed paste on.
def test_a_multi_line_message_goes_as_one_bracketed_block(phone):
    page, base = phone
    _stub_page(page)
    routes = []
    sent = _wire_sockets(page, routes=routes)
    _open_tile(page, base)

    # What a TUI does on startup. Inside the brackets it reads CR as a
    # literal newline instead of as submit, so the block lands whole and the
    # one CR outside them is what sends it.
    routes[0].send(b"\x1b[?2004h")
    page.wait_for_timeout(300)

    page.fill(BOX, "first line\nsecond line")
    page.click(SEND)
    page.wait_for_timeout(400)

    wire = _typed(sent)
    assert "\x1b[200~first line\rsecond line\x1b[201~\r" in wire, repr(wire)
    # One submit, not one per line. The whole point: a paragraph becomes a
    # single turn rather than three.
    # One line break inside the block, one CR after it. A third would mean a
    # line was submitted on its own.
    assert wire.count("\r") == 2, repr(wire)


# 7. With bracketed paste off, the message still goes as one submit rather
#    than being silently dropped.
def test_without_bracketed_paste_the_message_still_goes_unwrapped(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    # No ?2004h from this pane. The brackets would be typed as literal text,
    # so they must not be sent — but the message must still go.
    page.fill(BOX, "unbracketed all the same")
    page.click(SEND)
    page.wait_for_timeout(400)

    wire = _typed(sent)
    assert "unbracketed all the same\r" in wire
    assert "\x1b[200~" not in wire, "brackets sent to a pane that never asked"


# 8. After a send the socket accepted, the box is empty; after one it
#    refused, the text stays.
def test_the_box_clears_on_an_accepted_send_and_keeps_text_on_a_refused_one(phone):
    page, base = phone
    _stub_page(page)
    routes = []
    _wire_sockets(page, routes=routes)
    _open_tile(page, base)

    page.fill(BOX, "accepted message")
    page.click(SEND)
    page.wait_for_timeout(400)
    assert page.input_value(BOX) == ""

    # 1012 is another window taking the keyboard: the tile stands down for
    # good, so the socket is durably gone. The box stays typable through it on
    # purpose — the refusal comes from the socket, not from a lock.
    routes[0].close(code=1012)
    page.wait_for_timeout(800)
    assert page.get_attribute(BOX, "readonly") is None, "an outage locked the box"

    page.fill(BOX, "refused message")
    page.click(SEND)
    page.wait_for_timeout(400)
    assert page.input_value(BOX) == "refused message", "a refused send ate the text"
    # And says so. The arrow is live during an outage, so a tap that does
    # nothing and shows nothing reads as a dead button.
    assert "is-error" in (page.get_attribute(".term-compose", "class") or "")


# 9. If the connection turns out to have been dead, the submitted text comes
#    back into the box.
def test_a_submit_the_pane_never_answered_comes_back(phone):
    page, base = phone
    _stub_page(page)
    routes = []
    # A socket in front of nothing. The browser still reports it OPEN — on a
    # phone that state lasts up to fifteen seconds after a screen lock or a
    # wifi handoff, which is exactly when someone is composing.
    _wire_sockets(page, routes=routes, echo=False)
    _open_tile(page, base)

    page.fill(BOX, "swallowed by a dead socket")
    page.click(SEND)
    page.wait_for_timeout(200)
    assert page.input_value(BOX) == ""

    # Sending again is what a person does when nothing comes back. The second
    # submit must not discard the first one's only copy.
    page.fill(BOX, "and so was this one")
    page.click(SEND)
    page.wait_for_timeout(200)

    # A keepalive is not an answer. The pty end sends a NUL on a fixed cadence
    # whether or not anything is listening upstream, so counting it as an
    # acknowledgement drops the safety copy of a message that never arrived,
    # a few seconds after it didn't.
    routes[0].send(b"\x00")
    page.wait_for_timeout(300)

    # The socket was already gone; the browser only finds out now.
    routes[0].close(code=1001)
    page.wait_for_timeout(900)
    box = page.input_value(BOX)
    assert "swallowed by a dead socket" in box
    assert "and so was this one" in box

    # The other route to the same restore, and the one the box was actually
    # written for. A half-open socket never fires onclose at all — the browser
    # keeps reporting it OPEN — so the tile's own staleness watchdog is what
    # notices, and that path nulls onclose on its way past. STALE_MS is 15s,
    # polled every 2s; faking the clock also trips the reattach machinery this
    # is riding on, so the wait is real.
    page.fill(BOX, "")
    page.wait_for_timeout(200)
    page.fill(BOX, "typed into a zombie")
    page.click(SEND)
    page.wait_for_timeout(200)
    assert page.input_value(BOX) == ""
    page.wait_for_timeout(19000)
    assert "typed into a zombie" in page.input_value(BOX)


# 10. Typing directly into the terminal still reaches the session, and the box
#     never takes focus on its own.
def test_the_pane_is_still_directly_typable_and_the_box_does_not_steal_focus(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    # The box autofocusing on open is the plausible break: it swallows the
    # first keystrokes of someone who tapped the pane out of habit.
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.classList"
        ".contains('term-compose-input')"
    ) is False

    # A draft sitting in the box must not capture the pane's return key. The
    # y/n prompt is the case: tap the pane, type an answer, tap return, and a
    # box holding "a draft for later" would otherwise paste itself onto the
    # end of that line and submit "ya draft for later" as the answer.
    page.fill(BOX, "a draft for later")
    page.click(".term-panel .xterm-screen")
    page.keyboard.type("y")
    page.click(RETURN_KEY)
    page.wait_for_timeout(400)

    wire = _typed(sent)
    assert "y" in wire
    assert "a draft for later" not in wire, f"the draft hijacked the pane: {wire!r}"
    assert page.input_value(BOX) == "a draft for later", "the draft was consumed"


# 11. Unsent text returns after leaving the tile and coming back, and after a
#     reload.
def test_unsent_text_survives_leaving_the_tile_and_a_reload(phone):
    page, base = phone
    _stub_page(page)
    _wire_sockets(page)
    _open_tile(page, base)

    page.fill(BOX, "half a thought")
    page.wait_for_timeout(300)

    # Mobile's back arrow tears the panel down outright — it does not go
    # through closePanel, which is where a teardown-time save would have been
    # wired and would therefore never run.
    page.click(".term-panel-back")
    page.wait_for_timeout(300)
    page.query_selector_all(".term-card")[0].click()
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(500)
    assert page.input_value(BOX) == "half a thought"

    page.reload()
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(700)
    assert page.input_value(BOX) == "half a thought"


# 12. Switching to another session and back shows each session's own unsent
#     text.
def test_each_session_keeps_its_own_unsent_text(phone):
    page, base = phone
    _stub_page(page)
    _wire_sockets(page)
    _open_tile(page, base, index=0)

    page.fill(BOX, "for skippy")
    page.wait_for_timeout(300)
    page.click(".term-panel-back")
    page.wait_for_timeout(300)

    # Mobile is single-panel by construction: opening another session
    # destroys this one rather than tiling beside it.
    page.query_selector_all(".term-card")[1].click()
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(500)
    assert page.input_value(BOX) == "", "the other session's draft followed"
    page.fill(BOX, "for mordecai")
    page.wait_for_timeout(300)

    page.click(".term-panel-back")
    page.wait_for_timeout(300)
    page.query_selector_all(".term-card")[0].click()
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(500)
    assert page.input_value(BOX) == "for skippy"


# 13. When the session has ended or been evicted, the box refuses typing but
#     its text stays selectable.
def test_an_ended_session_locks_the_box_without_hiding_the_words(phone):
    page, base = phone
    _stub_page(page)
    routes = []
    _wire_sockets(page, routes=routes)
    _open_tile(page, base)

    page.fill(BOX, "never got to send this")
    page.wait_for_timeout(300)
    routes[0].close(code=4410)      # the gateway ended the session on purpose
    page.wait_for_timeout(800)

    assert page.get_attribute(BOX, "readonly") is not None
    # readOnly, never disabled: iOS refuses to let a disabled textarea be
    # selected or copied, and this text is the user's only copy.
    assert page.evaluate(f"() => document.querySelector('{BOX}').disabled") is False
    assert page.input_value(BOX) == "never got to send this"


# 14. An outage never locks the box: the text stays typable throughout and is
#     still there when the tile reconnects.
def test_an_outage_never_takes_the_keyboard_away(phone):
    page, base = phone
    _stub_page(page)
    routes = []
    _wire_sockets(page, routes=routes)
    _open_tile(page, base)

    page.fill(BOX, "waiting to be sent")
    page.wait_for_timeout(300)
    # 1012 stands the tile down for good — the longest outage there is, short
    # of the session itself ending. Locking here is what would drop the soft
    # keyboard mid-sentence, and composing through an outage is the entire
    # reason the box buffers at all.
    routes[0].close(code=1012)
    page.wait_for_timeout(800)
    assert page.get_attribute(BOX, "readonly") is None

    page.click(BOX)
    page.keyboard.type(" and still typing")
    assert page.input_value(BOX) == "waiting to be sent and still typing"

    # The state text is the reconnect button.
    page.click(".term-panel-info")
    page.wait_for_timeout(1200)
    assert page.get_attribute(BOX, "readonly") is None
    assert page.input_value(BOX) == "waiting to be sent and still typing"


# 15. A rotation to a successor session carries the unsent text with it.
def test_a_rotation_carries_the_unsent_text_to_the_successor(phone):
    page, base = phone
    _stub_page(page)
    routes = []
    _wire_sockets(page, routes=routes)
    _open_tile(page, base)

    page.fill(BOX, "mid-sentence when it rotated")
    page.wait_for_timeout(300)
    # 4412 retires this session for a successor whose id rides in the reason.
    routes[0].close(code=4412, reason="sess-b")
    page.wait_for_timeout(1200)

    assert page.input_value(BOX) == "mid-sentence when it rotated"
    assert page.evaluate(
        "() => localStorage.getItem('term-draft:sess-b')"
    ) == "mid-sentence when it rotated"
    # The stored draft moved rather than being copied. Re-deriving it from the
    # textarea would leave the retired session's key behind forever, and would
    # be wrong whenever the box is not the freshest copy — a submit still
    # awaiting its answer lives in storage, not on screen.
    assert page.evaluate("() => localStorage.getItem('term-draft:sess-a')") is None


# 16. The on-screen return key submits the box when the box has focus and
#     holds more than whitespace.
def test_the_key_rows_return_submits_the_focused_box(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    page.click(BOX)
    page.keyboard.type("sent by the key row")
    page.click(RETURN_KEY)
    page.wait_for_timeout(400)

    assert "sent by the key row" in _typed(sent)
    assert page.input_value(BOX) == ""


# 17. The on-screen return key sends a plain return when the box is empty or
#     whitespace only, and leaves the box alone.
def test_the_key_rows_return_still_sends_a_bare_return_for_a_blank_box(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    # A stray space is not a message. Answering a y/n prompt is what this key
    # is for, and spending it on a space is how that answer gets lost.
    page.click(BOX)
    page.keyboard.type("   ")
    page.click(RETURN_KEY)
    page.wait_for_timeout(400)

    assert _typed(sent).endswith("\r")
    assert "\x1b[200~" not in _typed(sent)
    assert page.input_value(BOX) == "   ", "a blank box was cleared as if sent"


# 18. Typing more lines than the box shows scrolls inside it and does not
#     change the terminal's size above it.
def test_a_long_message_scrolls_in_the_box_without_resizing_the_terminal(phone):
    page, base = phone
    _stub_page(page)
    _wire_sockets(page)
    _open_tile(page, base)

    before = page.evaluate(
        "() => document.querySelector('.term-xterm').getBoundingClientRect().height"
    )
    box_before = page.evaluate(
        f"() => document.querySelector('{BOX}').getBoundingClientRect().height"
    )
    page.fill(BOX, "\n".join("line %d" % i for i in range(12)))
    page.wait_for_timeout(500)

    after = page.evaluate(
        "() => document.querySelector('.term-xterm').getBoundingClientRect().height"
    )
    box_after = page.evaluate(
        f"() => document.querySelector('{BOX}').getBoundingClientRect().height"
    )
    # Shrinking a tmux pane discards history rather than reserving it, so a
    # box that grew with its content would quietly trim the transcript above
    # it, line by line, as the user typed.
    assert after == before, "the compose box resized the terminal"
    assert box_after == box_before, "the compose box grew instead of scrolling"
    assert page.evaluate(
        f"() => document.querySelector('{BOX}').scrollHeight > "
        f"document.querySelector('{BOX}').clientHeight"
    ) is True


# 19. Pressing a key-row button other than return leaves the box focused with
#     its text untouched.
def test_the_other_key_row_buttons_do_not_take_the_keyboard_from_the_box(phone):
    page, base = phone
    _stub_page(page)
    sent = _wire_sockets(page)
    _open_tile(page, base)

    page.click(BOX)
    page.keyboard.type("a follow-up while it works")
    # Ctrl+C is the one that matters: interrupting a running turn is exactly
    # what someone does while composing a follow-up, and a button that steals
    # focus drops the soft keyboard and the draft with it.
    page.click(INTERRUPT_KEY)
    page.wait_for_timeout(400)

    assert "\x03" in _typed(sent), "the interrupt never reached the pty"
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.classList"
        ".contains('term-compose-input')"
    ) is True
    assert page.input_value(BOX) == "a follow-up while it works"
