/* Reattach routing rules for the web terminal.
 *
 * Run by tests/test_terminal_routing.py. Every case here is a shape that
 * actually broke: tiles adopting each other's conversations, labels
 * following the wrong session, and a live session declared dead because
 * its socket blinked.
 */
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
// The module is a browser script, not an ES module — evaluate it the way a
// <script> tag would, against globalThis.
new Function(readFileSync(join(here, "..", "..", "src", "static", "terminal-routing.js"), "utf8"))();
const {
    pickReattachTarget, isActive, retryDelayMs, socketIsStale, attemptsAfterSocket, attachExhausted, contrastText, pendingImeText, shouldResetImeAccumulator, pageScrollLines,
    pageScrollAction, wheelReport, dragWheelSteps, parseOpenFragment, formatOpenFragment,
} = globalThis.TerminalRouting;

const tests = {
    "a live session is retried, not replaced"() {
        const rows = [{id: "a", status: "running"}];
        assert.deepEqual(pickReattachTarget(rows, {deadId: "a", openIds: ["a"]}), {action: "retry"});
    },

    "an idle session still counts as live"() {
        const rows = [{id: "a", status: "idle"}];
        assert.equal(pickReattachTarget(rows, {deadId: "a", openIds: []}).action, "retry");
    },

    "a rotation successor is adopted by lineage"() {
        const rows = [
            {id: "a", status: "closed"},
            {id: "b", status: "running", rotated_from: "a"},
        ];
        const target = pickReattachTarget(rows, {deadId: "a", openIds: ["a"]});
        assert.equal(target.action, "successor");
        assert.equal(target.session.id, "b");
    },

    "a sibling session on the same mind is NEVER adopted"() {
        // The exact bug: two terminals open, one drops, and it grabs the
        // other one's conversation along with its name and colour.
        const rows = [
            {id: "hex", status: "closed", mind_id: "skippy"},
            {id: "ada-proxy", status: "running", mind_id: "skippy"},
        ];
        assert.deepEqual(
            pickReattachTarget(rows, {deadId: "hex", openIds: ["hex", "ada-proxy"]}),
            {action: "wait"},
        );
    },

    "a successor already open in another tile is left alone"() {
        const rows = [
            {id: "a", status: "closed"},
            {id: "b", status: "running", rotated_from: "a"},
        ];
        assert.equal(
            pickReattachTarget(rows, {deadId: "a", openIds: ["a", "b"]}).action,
            "wait",
        );
    },

    "a closed successor is not adopted"() {
        const rows = [
            {id: "a", status: "closed"},
            {id: "b", status: "closed", rotated_from: "a"},
        ];
        assert.equal(pickReattachTarget(rows, {deadId: "a", openIds: []}).action, "wait");
    },

    "lineage pointing at a different session does not match"() {
        const rows = [
            {id: "a", status: "closed"},
            {id: "b", status: "running", rotated_from: "someone-else"},
        ];
        assert.equal(pickReattachTarget(rows, {deadId: "a", openIds: []}).action, "wait");
    },

    "retry wins over an equally valid successor"() {
        // If the original is still alive, the conversation never moved.
        const rows = [
            {id: "a", status: "running"},
            {id: "b", status: "running", rotated_from: "a"},
        ];
        assert.equal(pickReattachTarget(rows, {deadId: "a", openIds: []}).action, "retry");
    },

    "empty and malformed inputs wait rather than guess"() {
        assert.equal(pickReattachTarget([], {deadId: "a", openIds: []}).action, "wait");
        assert.equal(pickReattachTarget(null, {deadId: "a", openIds: []}).action, "wait");
        assert.equal(pickReattachTarget([{id: "a", status: "running"}], {}).action, "wait");
        assert.equal(pickReattachTarget([null, undefined], {deadId: "a"}).action, "wait");
    },

    "retries back off and cap instead of spinning"() {
        assert.equal(retryDelayMs(1), 500);
        assert.equal(retryDelayMs(2), 1000);
        assert.equal(retryDelayMs(3), 2000);
        // A mind that refuses every handshake must settle at the cap, not
        // keep reconnecting several times a second.
        assert.equal(retryDelayMs(20), 8000);
        // Garbage in still yields a real wait.
        assert.equal(retryDelayMs(0), 500);
        assert.equal(retryDelayMs(undefined), 500);
    },

    "contrastText picks dark ink on light session colors"() {
        assert.equal(contrastText("#ffffff"), "#0b1724");
        assert.equal(contrastText("#ffce42"), "#0b1724");
        assert.equal(contrastText("#d8dee4"), "#0b1724");
    },

    "contrastText picks light ink on dark session colors"() {
        assert.equal(contrastText("#0b1724"), "#f4f8fb");
        assert.equal(contrastText("#e05c8a"), "#f4f8fb");
        assert.equal(contrastText("#3d5266"), "#f4f8fb");
    },

    "contrastText falls back to the neutral ink on garbage"() {
        // Painted headers and cards call this with whatever the label
        // store holds — an empty label must not paint black-on-black.
        assert.equal(contrastText(""), "#dce8f0");
        assert.equal(contrastText(null), "#dce8f0");
        assert.equal(contrastText("red"), "#dce8f0");
        assert.equal(contrastText("#12345"), "#dce8f0");
    },

    "isActive classifies the statuses the rail renders"() {
        assert.equal(isActive({status: "RUNNING"}), true);
        assert.equal(isActive({status: "idle"}), true);
        assert.equal(isActive({status: "closed"}), false);
        assert.equal(isActive({}), false);
        assert.equal(isActive(null), false);
    },

    "a page of scrollback keeps two rows of overlap"() {
        assert.equal(pageScrollLines(24), 22);
        assert.equal(pageScrollLines(40.7), 38);
    },

    "a tile too short to overlap still moves"() {
        // Never 0: a button that does nothing reads as a broken terminal.
        assert.equal(pageScrollLines(2), 1);
        assert.equal(pageScrollLines(1), 1);
        assert.equal(pageScrollLines(0), 1);
        assert.equal(pageScrollLines(undefined), 1);
    },

    "a settled IME box owes nothing to Enter"() {
        // The whole paragraph is still sitting in xterm's helper textarea
        // because xterm never clears it between words, but every word went
        // to the pty as its own composition ended. Forwarding it again is
        // what doubled Daniel's typed text.
        const settled = {composing: false, flushPending: false, start: 0, alreadySent: ""};
        assert.equal(pendingImeText(settled, "the whole paragraph"), "");
    },

    "an open composition is forwarded from its own start"() {
        // "hello " already went out; "world" is mid-composition and would
        // be discarded by xterm's Enter path.
        const open = {composing: true, flushPending: false, start: 6, alreadySent: ""};
        assert.equal(pendingImeText(open, "hello world"), "world");
    },

    "a composition whose flush Enter is about to cancel is forwarded"() {
        const inFlight = {composing: false, flushPending: true, start: 6, alreadySent: ""};
        assert.equal(pendingImeText(inFlight, "hello world"), "world");
    },

    "text sent after the composition recorded its start is not resent"() {
        // xterm's non-composition path can deliver characters between
        // compositionstart and the flush; it corrects the offset by their
        // length and so must this.
        const state = {composing: true, flushPending: false, start: 6, alreadySent: "wo"};
        assert.equal(pendingImeText(state, "hello world"), "rld");
    },

    "the IME accumulator is emptied once a burst has committed"() {
        // Between words the box is settled; clearing it stops the next
        // keystroke from diffing against a line the pty already holds.
        assert.equal(shouldResetImeAccumulator({composing: false}), true);
    },

    "the IME accumulator is left alone mid-composition"() {
        // The word being typed still lives in the box; clearing it now drops
        // the in-progress word.
        assert.equal(shouldResetImeAccumulator({composing: true}), false);
    },

    "an unknown composition state is left untouched"() {
        assert.equal(shouldResetImeAccumulator(null), false);
        assert.equal(shouldResetImeAccumulator(undefined), false);
    },

    "on the alternate buffer a page of scrolling is the program's to do"() {
        // The whole bug: xterm's scrollback does not exist on the alt
        // buffer, so scrolling it locally moved nothing at all.
        assert.deepEqual(
            pageScrollAction({altBuffer: true, rows: 27, dir: -1}),
            {kind: "bytes", data: "\x1b[5~"},
        );
        assert.deepEqual(
            pageScrollAction({altBuffer: true, rows: 27, dir: 1}),
            {kind: "bytes", data: "\x1b[6~"},
        );
    },

    "on the normal buffer the tile scrolls its own scrollback"() {
        assert.deepEqual(
            pageScrollAction({altBuffer: false, rows: 27, dir: -1}),
            {kind: "lines", lines: -25},
        );
        assert.deepEqual(
            pageScrollAction({altBuffer: false, rows: 27, dir: 1}),
            {kind: "lines", lines: 25},
        );
    },

    "a wheel notch is an SGR report the TUI already listens for"() {
        assert.equal(wheelReport(-1, 12, 5), "\x1b[<64;12;5M");
        assert.equal(wheelReport(1, 12, 5), "\x1b[<65;12;5M");
        // Cells are 1-based; a pointer measured off the top-left edge
        // must not report a zeroth column.
        assert.equal(wheelReport(-1, 0, -3), "\x1b[<64;1;1M");
    },

    "a finger moving down pulls older lines into view"() {
        assert.deepEqual(dragWheelSteps(60, 24), {steps: 2, dir: -1, consumed: 48});
        assert.deepEqual(dragWheelSteps(-60, 24), {steps: 2, dir: 1, consumed: -48});
    },

    "drag distance short of a notch is carried, not lost"() {
        // Slow drags are all short moves; discarding each one would make
        // the tile ignore the gesture entirely.
        assert.deepEqual(dragWheelSteps(20, 24), {steps: 0, dir: 1, consumed: 0});
        assert.equal(dragWheelSteps(0, 24).steps, 0);
    },

    "the open fragment round-trips a set of session ids"() {
        assert.equal(formatOpenFragment(["a", "b"]), "#s=a,b");
        assert.deepEqual(parseOpenFragment("#s=a,b"), ["a", "b"]);
        assert.deepEqual(parseOpenFragment(formatOpenFragment(["one", "two"])), ["one", "two"]);
    },

    "an empty open set carries no fragment"() {
        // A tile-less stage must leave a clean URL, not a bare #s=.
        assert.equal(formatOpenFragment([]), "");
        assert.equal(formatOpenFragment(["", null]), "");
        assert.deepEqual(parseOpenFragment(""), []);
        assert.deepEqual(parseOpenFragment("#"), []);
        assert.deepEqual(parseOpenFragment("#other=1"), []);
    },

    "fragment parsing tolerates a leading hash or none, and stray commas"() {
        assert.deepEqual(parseOpenFragment("s=a,b"), ["a", "b"]);
        assert.deepEqual(parseOpenFragment("#s=a,,b, "), ["a", "b"]);
        assert.deepEqual(parseOpenFragment(null), []);
    },

    "an empty or exhausted box owes nothing"() {
        const open = {composing: true, flushPending: false, start: 11, alreadySent: ""};
        assert.equal(pendingImeText(open, "hello world"), "");
        assert.equal(pendingImeText(open, ""), "");
        assert.equal(pendingImeText(open, null), "");
        assert.equal(pendingImeText(null, "hello"), "");
    },

    "a socket silent past the threshold is stale"() {
        // Keepalive is 5s; 15s (three missed beats) is the watchdog cutoff.
        assert.equal(socketIsStale(100000, 100000 - 16000, 15000), true);
        assert.equal(socketIsStale(100000, 100000 - 3000, 15000), false);
        // Foreground return uses a tighter 6s so a rotation reattaches fast.
        assert.equal(socketIsStale(100000, 100000 - 7000, 6000), true);
    },

    "a socket that dies right after opening keeps climbing the backoff"() {
        // Evicted a second after connecting: that attach is failing, so the
        // count carries and retryDelayMs stretches the next try.
        assert.equal(attemptsAfterSocket(3, 1000, 10000), 3);
        assert.ok(retryDelayMs(attemptsAfterSocket(3, 1000, 10000)) >= 2000);
    },

    "a socket that held clears the backoff"() {
        assert.equal(attemptsAfterSocket(3, 30000, 10000), 0);
        // A never-opened socket counts as a zero-length life, not a success.
        assert.equal(attemptsAfterSocket(2, 0, 10000), 2);
    },

    "a refused attach stops trying at the cap"() {
        // A live session that refuses every attach keeps pickReattachTarget
        // answering "retry", so only the cap ends the loop.
        assert.equal(attachExhausted(5, 6), false);
        assert.equal(attachExhausted(6, 6), true);
        assert.equal(attachExhausted(0, 6), false);
    },

    "a socket with nothing received yet is never called dead"() {
        // lastRecvAt 0 means the socket just opened; leave it to onopen/onclose.
        assert.equal(socketIsStale(100000, 0, 15000), false);
    },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
    try {
        fn();
        console.log("ok - " + name);
    } catch (err) {
        failed++;
        console.error("FAIL - " + name + "\n  " + err.message);
    }
}
console.log(`\n${Object.keys(tests).length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
