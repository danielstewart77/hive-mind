/* Reattach routing for the web terminal.
 *
 * When a terminal tile's socket drops it has to decide what to reconnect
 * to. Getting this wrong is not cosmetic: the previous rule was "any live
 * session on the same mind", so with two tiles open the reconnecting one
 * adopted its neighbour's conversation, took its neighbour's name and
 * colour with it, and left the neighbour blank.
 *
 * The rules, in order:
 *   1. The same session, if it is still live. The pty now outlives the
 *      socket, so this is the ordinary case — a dropped connection, not a
 *      dead conversation.
 *   2. The session that rotation created to replace it, identified by
 *      recorded lineage (rotated_from) and not by proximity.
 *   3. Nothing. Waiting beats stealing.
 *
 * A session another tile already has open is never a candidate.
 */
(function (root) {
    "use strict";

    var ACTIVE_STATUSES = ["running", "idle"];

    function isActive(row) {
        return ACTIVE_STATUSES.indexOf(String(row && row.status || "").toLowerCase()) !== -1;
    }

    /**
     * @param {Array} rows      session rows from /api/terminal/sessions
     * @param {Object} opts     {deadId, openIds}
     * @returns {{action: "retry"|"successor"|"wait", session?: Object}}
     */
    function pickReattachTarget(rows, opts) {
        var list = Array.isArray(rows) ? rows : [];
        var deadId = (opts && opts.deadId) || "";
        var openIds = (opts && opts.openIds) || [];
        if (!deadId) return {action: "wait"};

        var i;
        for (i = 0; i < list.length; i++) {
            if (list[i] && list[i].id === deadId && isActive(list[i])) {
                return {action: "retry"};
            }
        }
        for (i = 0; i < list.length; i++) {
            var row = list[i];
            if (!row || row.id === deadId) continue;
            if (!row.rotated_from || row.rotated_from !== deadId) continue;
            if (!isActive(row)) continue;
            // Another tile is already driving this one — adopting it would
            // put two terminals on one conversation.
            if (openIds.indexOf(row.id) !== -1) continue;
            return {action: "successor", session: row};
        }
        return {action: "wait"};
    }

    /**
     * Delay before retrying an attach to the same session, in ms.
     *
     * The first retry is quick — the common case is a dropped socket over
     * a live pty, and waiting there is felt. Repeated failures mean the
     * attach itself is being refused, so the interval grows and caps
     * rather than spinning at the speed of the round trip.
     *
     * @param {number} attempt  1 for the first retry after a live socket
     * @returns {number} ms to wait
     */
    function retryDelayMs(attempt) {
        var n = Math.max(1, Math.floor(attempt) || 1);
        return Math.min(500 * Math.pow(2, n - 1), 8000);
    }

    /**
     * Is an attached socket a zombie? A mobile rotation or a network blip
     * leaves the TCP connection half-open: the browser keeps reporting the
     * socket OPEN and does not fire onclose until its own ~30s timeout, so
     * the tile sits black long after the pty is reachable again. The pty end
     * sends a keepalive on a fixed cadence, so a gap since the last received
     * byte that exceeds a few of those beats means the connection is dead
     * even though it still claims to be open — reattach now instead of
     * waiting for the browser to notice.
     *
     * @param {number} nowMs        Date.now()
     * @param {number} lastRecvAt   ms timestamp of the last byte received
     * @param {number} thresholdMs  silence tolerated before declaring death
     * @returns {boolean}
     */
    function socketIsStale(nowMs, lastRecvAt, thresholdMs) {
        if (!lastRecvAt) return false;   // nothing received yet — not our call
        return (nowMs - lastRecvAt) > thresholdMs;
    }

    /**
     * Whether a close code is a decision rather than a blip, and what to say.
     *
     * The tile's fallback on an unrecognised code is to reattach with backoff,
     * which is right for a dropped socket and wrong for every code in here:
     * the mind has answered, its answer will not change by asking again, and a
     * tile that keeps asking shows "reattaching..." forever over a refusal
     * nobody can see. Each message names the remedy, because these are
     * different problems with the same symptom — an image to rebuild, a
     * credential the broker is holding stale, a tile already open elsewhere.
     *
     * @param {number} code  WebSocket close code
     * @returns {{message: string}|null} null when the tile should retry
     */
    function permanentClose(code) {
        switch (code) {
            // Another tile took the keyboard. Reconnecting is a tug-of-war.
            case 1012: return {message: "open in another window"};
            // The mind took the attach and then refused it: no conversation to
            // resume, or a harness with no pty.
            case 1008: return {message: "mind refused the terminal"};
            // Reachable, but its image predates the pty route.
            case 4415: return {message: "no terminal on this mind"};
            // Has the route, and rejected the gateway's credential. Same
            // shape as 4415, different fix — the broker holds a token this
            // mind no longer accepts, or none at all.
            case 4416: return {message: "mind refused the gateway's credential"};
            // Two sources, one remedy that is not reattaching: the mind's own
            // fallback when its server has no denial-response extension, and
            // this site when the browser's sign-in has ended (signed out,
            // disabled, reset) — at attach or while the socket is open.
            case 4401: return {message: "credential refused — sign in again"};
            // This site's own refusals, sent after accepting the socket so the
            // browser sees the code: a session on a mind this user does not
            // own (or no longer owns), or a temporary password still to be
            // changed. Asking again gets the same no.
            case 4403: return {message: "not yours to open"};
            case 4428: return {message: "change your password first"};
            default: return null;
        }
    }

    /**
     * Consecutive-failure count to carry into the next reattach, given how
     * long the socket that just died stayed up.
     *
     * A socket that opens and dies straight back is a failing attach, not a
     * recovered one, however clean its handshake looked — so its attempt
     * count keeps climbing and `retryDelayMs` backs the tile off. Only a
     * socket that held for a while proves the attach works and clears the
     * count. Resetting on every open instead lets a tile that is being
     * evicted as fast as it connects retry twice a second forever.
     *
     * @param {number} attempts    consecutive failures so far
     * @param {number} lifetimeMs  how long the socket that just died was open
     * @param {number} stableMs    uptime that counts as a healthy attach
     * @returns {number} attempts to carry forward
     */
    function attemptsAfterSocket(attempts, lifetimeMs, stableMs) {
        return lifetimeMs >= stableMs ? 0 : (attempts || 0);
    }

    /**
     * Has a tile retried enough that it should stop and wait to be asked?
     *
     * The successor hunt only ends the reattach loop when the session is
     * gone. A session that exists but refuses to attach — a mind with no
     * pty, a harness that closes the socket back — therefore never ends it
     * at all, and the tile retries for as long as the tab is open. The cap
     * is what turns that into a stopped tile with a visible way back.
     *
     * @param {number} attempts     consecutive failures, including this one
     * @param {number} maxAttempts  cap before standing down
     * @returns {boolean}
     */
    function attachExhausted(attempts, maxAttempts) {
        return (attempts || 0) >= maxAttempts;
    }

    /**
     * Readable ink color against an arbitrary swatch background, by
     * perceived luminance. Shared by the focused tile's full-bar header
     * and the rail's painted picker cards, so both surfaces flip their
     * text the same way for the same session color.
     *
     * @param {string} hex  "#rrggbb"
     * @returns {string} a dark or light ink hex; a neutral default for
     *                   anything that isn't a 6-digit hex color
     */
    function contrastText(hex) {
        var c = String(hex || "").replace("#", "");
        if (!/^[0-9a-fA-F]{6}$/.test(c)) return "#dce8f0";
        var r = parseInt(c.slice(0, 2), 16);
        var g = parseInt(c.slice(2, 4), 16);
        var b = parseInt(c.slice(4, 6), 16);
        var lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
        return lum > 0.6 ? "#0b1724" : "#f4f8fb";
    }

    /**
     * The slice of xterm's helper textarea that Enter has to forward by
     * hand, given xterm's own composition state.
     *
     * Mobile keyboards type into that textarea through IME compositions,
     * and xterm does not clear it between words — it holds the whole line
     * and each composition is sent as it ends, from its own start offset
     * to the end of the box. So by the time Enter arrives, everything up
     * to the open composition's start is already down the wire. Forwarding
     * the whole box (the first cut of the mobile-Enter fix) resends the
     * entire paragraph and the pty shows it twice.
     *
     * Nothing is owed when no composition is open or in flight: xterm has
     * already sent it all. When one is, xterm is about to either drop it
     * (Enter finalizes with commit=false) or slice it against a stale end
     * offset, so this is the text that would otherwise be lost.
     *
     * @param {Object} state  xterm's composition state:
     *                        {composing, flushPending, start, alreadySent}
     * @param {string} value  the helper textarea's current value
     * @returns {string} the text to send before the carriage return
     */
    function pendingImeText(state, value) {
        var text = String(value == null ? "" : value);
        if (!state || !text) return "";
        if (!state.composing && !state.flushPending) return "";
        // Text the non-composition path sent after the composition
        // recorded its start offset sits between the two; xterm applies
        // the same correction when it flushes.
        var start = (state.start || 0) + String(state.alreadySent || "").length;
        if (!(start >= 0) || start >= text.length) return "";
        return text.slice(start);
    }

    /**
     * Whether xterm's hidden composition textarea should be emptied now that
     * a burst of input has been committed to the pty.
     *
     * The textarea is xterm's model of "the current line", diffed against its
     * previous value on every change (grew -> send the appended tail, shrank
     * -> send one DEL). But a TUI like claude owns the real line, not xterm.
     * Left to accumulate, the box carries the whole line, and a mobile
     * keyboard's autocorrect, prediction, or multi-character backspace
     * rewrites text the pty already has — the diff then re-slices that stale
     * region and echoes it a second time (the "keys repeat, deleted
     * characters come back" bug). Emptying the box after each committed burst
     * makes the next keystroke diff from nothing, so nothing can be re-sent.
     *
     * Never mid-composition: while a word is still being typed the box holds
     * it, and clearing would drop the word in progress.
     *
     * @param {Object} state {composing: boolean}
     * @returns {boolean} true when the accumulator should be reset to empty
     */
    function shouldResetImeAccumulator(state) {
        return !!(state && state.composing !== true);
    }

    /**
     * The bytes one submit of the mobile compose box puts on the wire.
     *
     * The box exists because a phone keyboard's autocorrect, prediction and
     * voice typing all work by rewriting text that is already in the field —
     * text xterm would have forwarded to the pty keystroke by keystroke, and
     * that re-send is the duplicated-sentence report. Nothing composed here
     * reaches the pty until this function runs, so there is no already-sent
     * region to rewrite.
     *
     * A multi-line message has to arrive as one message. Inside a bracketed
     * paste the TUI reads a CR as a literal newline rather than as submit, so
     * the whole block lands in the composer and the single trailing CR — sent
     * outside the brackets — is what submits it.
     *
     * Without the mode there is nothing to wrap with: the brackets would be
     * typed as text. The newlines still become CR, which a line-oriented app
     * submits one line at a time. That is the app's own paste behaviour, and
     * inventing something else here would be worse than matching it.
     *
     * @param {string} text        the box's contents
     * @param {boolean} bracketed  whether the app has bracketed paste on
     *                             (xterm's `term.modes.bracketedPasteMode`)
     * @returns {string} bytes to send, or "" when there is nothing to submit
     */
    function composeSubmitBytes(text, bracketed) {
        // Trailing newlines are what a return-key-inserts-a-newline box
        // collects on the way to the arrow; they are not part of the message,
        // and inside the brackets each one is a blank line in the composer.
        if (!composeIsSubmittable(text)) return "";
        var body = String(text).replace(/\r?\n/g, "\r").replace(/\r+$/, "");
        return bracketed ? "\x1b[200~" + body + "\x1b[201~\r" : body + "\r";
    }

    /**
     * Is there a message in the box, as opposed to leftover whitespace?
     *
     * The toolbar's return key reads this to decide whether it is submitting
     * the box or sending a plain CR to the pane. A box holding one stray
     * space would otherwise capture the only key the user has for answering a
     * y/n prompt they typed straight into the terminal, and answer it with a
     * space.
     *
     * @param {string} text  the box's contents
     * @returns {boolean}
     */
    function composeIsSubmittable(text) {
        return String(text == null ? "" : text).trim() !== "";
    }

    /**
     * Whether this browser gets a compose box at all.
     *
     * Width alone is the wrong question. The box exists for on-screen
     * keyboards, and a phone in landscape is 915-932 CSS px — wider than the
     * 859px the rest of the tile's mobile machinery keys off. Gating on width
     * would take the box away on rotation and hand the duplication straight
     * back, on the same device, mid-sentence. A coarse pointer with no hover
     * is what actually names the devices that autocorrect.
     *
     * Either condition is enough: the narrow arm keeps a desktop window
     * dragged small behaving like the rest of the mobile layout, and the
     * pointer arm covers the landscape phone the narrow arm misses.
     *
     * @param {Object} env  {narrow: boolean, coarsePointer: boolean}
     * @returns {boolean}
     */
    function composeBoxWanted(env) {
        var e = env || {};
        return !!(e.narrow || e.coarsePointer);
    }

    /**
     * Whether opening a tile should put the cursor in the pane itself.
     *
     * Deliberately *not* `composeBoxWanted`. That one is `narrow ||
     * coarsePointer`, and its narrow arm exists for a desktop window dragged
     * small — a mouse at 800px, or a 1280px window at 175% zoom, which
     * shrinks the CSS viewport the same way. Reusing it here would stop a
     * perfectly ordinary desktop window accepting typing at all: nothing
     * focused, no keydown listener on the page to catch the keystrokes, and
     * the only cue an unfocused xterm cursor, which is an outline instead of
     * a block.
     *
     * The question here is only ever "does this device paint a keyboard over
     * the page", because that keyboard is what the cursor costs: a focused
     * editable is what a phone re-raises its keyboard for, on any later tap
     * anywhere. So it is the pointer arm alone, and width never enters.
     *
     * A touch device still reaches the pane by tapping it — xterm focuses its
     * own textarea on a tap. What this withholds is the *automatic* focus
     * nobody asked for.
     *
     * @param {Object} env  {coarsePointer: boolean}
     * @returns {boolean}
     */
    function paneTakesFocusOnOpen(env) {
        var e = env || {};
        return !e.coarsePointer;
    }

    /**
     * Whether the box should refuse typing, given the tile's attach state.
     *
     * Only the two states where the conversation itself is gone: ended, and
     * suspended. Not a connection state, and deliberately not standing down.
     *
     * `readOnly` on a focused textarea drops the soft keyboard, so locking is
     * how a phone loses a half-typed message. Every connection state is a gap
     * the tile closes by itself — and standing down, which follows ~25s of
     * failed reattach, is the *longest* of them: a lift, a tunnel, a wifi
     * handoff. Composing through an outage is the entire reason a buffer
     * exists, so that is the last state that should take the keyboard away.
     * A submit attempted there is refused and the words stay put.
     *
     * Read-only rather than disabled, deliberately. A session ended for good
     * never re-enables, and iOS will not let a disabled textarea be selected
     * or copied — the user's own unsent words would be visible, permanent and
     * unrecoverable.
     *
     * @param {Object} state  {attachState: string}
     * @returns {boolean}
     */
    function composeLocked(state) {
        var s = state || {};
        var name = String(s.attachState || "");
        return name === "session ended" || name === "session suspended";
    }

    /**
     * How far one press of the tile's PgUp/PgDn moves the viewport.
     *
     * A whole screen at a time loses the reader's place, so two rows of
     * overlap carry over. A tile too short for that still has to move by
     * something, hence the floor of one.
     *
     * @param {number} rows  the terminal's current row count
     * @returns {number} lines to scroll, always at least 1
     */
    function pageScrollLines(rows) {
        var n = Math.floor(Number(rows)) || 0;
        return Math.max(1, n - 2);
    }

    /**
     * Where a page of scrolling has to be done: here, or by the pane.
     *
     * The pane is tmux, and tmux is where the scrollback lives — 50,000
     * lines of it. The alternate screen the tile sees is tmux's own, so
     * xterm has no scrollback to move and the page keys are no use
     * either: tmux forwards them to the program, which has nothing behind
     * it to show. Only a wheel report reaches tmux's history, so a page
     * press is a burst of wheel notches, the same channel a finger drag
     * uses. tmux moves about five lines per notch and leaves copy-mode on
     * its own once the burst reaches the bottom again. On the normal
     * buffer the tile owns the scrollback and scrolls it locally.
     *
     * @param {Object} state  {altBuffer: boolean, rows: number, dir: -1|1}
     * @returns {{kind: "wheel", notches: number, dir: -1|1}
     *          |{kind: "lines", lines: number}}
     */
    function pageScrollAction(state) {
        var dir = Number(state && state.dir) < 0 ? -1 : 1;
        if (state && state.altBuffer) {
            var rows = Math.floor(Number(state.rows)) || 0;
            return {kind: "wheel", notches: Math.max(1, Math.round(rows / 5)), dir: dir};
        }
        return {kind: "lines", lines: dir * pageScrollLines(state && state.rows)};
    }

    /**
     * One wheel notch as an SGR (1006) mouse report.
     *
     * Buttons 64 and 65 are wheel-up and wheel-down; the TUI enabled this
     * encoding itself, so it is the channel it already listens on.
     *
     * @param {-1|1} dir      -1 scrolls back through history, 1 forward
     * @param {number} col    1-based cell column of the pointer
     * @param {number} row    1-based cell row of the pointer
     */
    function wheelReport(dir, col, row) {
        var button = Number(dir) < 0 ? 64 : 65;
        var x = Math.max(1, Math.floor(Number(col) || 1));
        var y = Math.max(1, Math.floor(Number(row) || 1));
        return "\x1b[<" + button + ";" + x + ";" + y + "M";
    }

    /**
     * A touch drag, in wheel notches.
     *
     * A phone has no wheel and, on the alternate buffer, nothing for the
     * browser to pan either, so a drag has to be converted by hand. A
     * finger moving down pulls older lines into view, which is a wheel-up
     * notch. Whatever distance doesn't fill a whole notch is returned so
     * the caller can carry it into the next move rather than losing it.
     *
     * @param {number} deltaY         pixels moved since the last report
     * @param {number} pixelsPerStep  drag distance one notch is worth
     * @returns {{steps: number, dir: -1|1, consumed: number}}
     */
    function dragWheelSteps(deltaY, pixelsPerStep) {
        var step = Math.abs(Number(pixelsPerStep)) || 24;
        var n = Math.trunc((Number(deltaY) || 0) / step);
        return {steps: Math.abs(n), dir: n > 0 ? -1 : 1, consumed: n * step};
    }

    /**
     * The session ids named in a URL fragment, in order.
     *
     * The fragment is the shareable, per-tab record of what a tile is
     * showing: `#s=id1,id2`. A fragment because it never reaches the
     * server, so it rides through Cloudflare and the reverse proxy
     * untouched and needs no route. Each browser tab carries its own, so
     * two tabs restore independently with no coordination.
     *
     * @param {string} hash  location.hash, with or without the leading #
     * @returns {string[]}   ids, empty on anything malformed
     */
    function parseOpenFragment(hash) {
        try {
            var m = /(?:^|[#&])s=([^&]*)/.exec(String(hash || ""));
            if (!m) return [];
            return decodeURIComponent(m[1])
                .split(",")
                .map(function (s) { return s.trim(); })
                .filter(Boolean);
        } catch (e) {
            return [];
        }
    }

    /**
     * The fragment string for a set of open session ids.
     *
     * Empty in, empty out — a tile-less stage carries no fragment rather
     * than a bare `#s=`, so closing the last tile leaves a clean URL.
     *
     * @param {string[]} ids
     * @returns {string}  e.g. "#s=a,b" or ""
     */
    function formatOpenFragment(ids) {
        var list = (ids || []).filter(Boolean);
        return list.length ? "#s=" + list.map(encodeURIComponent).join(",") : "";
    }

    /**
     * Tear a terminal socket down so it can never write again.
     *
     * `close()` is asynchronous: the socket enters CLOSING and any frames
     * already in flight still fire `onmessage` until the handshake
     * finishes. A reattach that only closes the old socket therefore has a
     * window where two sockets are live at once, and both call
     * `term.write()` into the same terminal.
     *
     * That window is what corrupted the screen. Each socket carries a
     * separate tmux client's update stream, and a tmux client emits
     * cursor-addressed deltas against its own model of what the terminal
     * already shows — it writes "move right two columns" where it believes
     * blank cells sit rather than spending bytes on spaces. Interleave two
     * such streams and each one's cursor moves land inside the other's,
     * leaving cells neither client will ever write again. The buffer ends
     * up holding text no repaint can reclaim, because both clients are
     * certain those cells are already correct.
     *
     * So the handlers come off first and the close comes second. A socket
     * with no `onmessage` cannot reach the terminal no matter how many
     * frames the browser still has queued for it.
     *
     * @param {WebSocket|null} socket
     * @returns {boolean}  whether there was a socket to detach
     */
    function detachSocket(socket) {
        if (!socket) return false;
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        socket.onopen = null;
        try {
            socket.close();
        } catch (err) {
            /* already closing or closed — the handlers are off either way */
        }
        return true;
    }

    /* What a console shows and what it says are not the same text.
     *
     * Terminal replies are written for eyes — fenced code, paths, numbered
     * lists — because that is what a console should get. Read aloud
     * verbatim it is a minute of backticks and slashes. This runs only on
     * the copy bound for the voice server, so the screen keeps every
     * character exactly as written.
     */
    function speakable(text) {
        return String(text == null ? "" : text)
            /* A fenced block goes whole, language tag and all. An
             * unterminated fence takes the rest of the chunk with it: a
             * block still being written is not prose that happens to be
             * missing its closing fence. */
            .replace(/```[\s\S]*?(```|$)/g, " ")
            .replace(/`([^`]*)`/g, "$1")
            /* Leading markers, never the item's own words. */
            .replace(/^[ \t]*[-*+][ \t]+/gm, "")
            .replace(/^[ \t]*\d+[.)][ \t]+/gm, "")
            .replace(/^[ \t]*>[ \t]?/gm, "")
            .replace(/^[ \t]*#{1,6}[ \t]+/gm, "")
            /* Emphasis and table rules read as punctuation otherwise. */
            .replace(/(\*\*|__)/g, "")
            .replace(/(^|[\s(])[*_]([^*_\n]+)[*_]($|[\s.,;:!?)])/g, "$1$2$3")
            .replace(/\|/g, " ")
            .replace(/[ \t]{2,}/g, " ")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
    }

    root.TerminalRouting = {
        speakable: speakable,
        detachSocket: detachSocket,
        parseOpenFragment: parseOpenFragment,
        formatOpenFragment: formatOpenFragment,
        pageScrollLines: pageScrollLines,
        pageScrollAction: pageScrollAction,
        wheelReport: wheelReport,
        dragWheelSteps: dragWheelSteps,
        pickReattachTarget: pickReattachTarget,
        isActive: isActive,
        retryDelayMs: retryDelayMs,
        socketIsStale: socketIsStale,
        permanentClose: permanentClose,
        attemptsAfterSocket: attemptsAfterSocket,
        attachExhausted: attachExhausted,
        contrastText: contrastText,
        pendingImeText: pendingImeText,
        shouldResetImeAccumulator: shouldResetImeAccumulator,
        composeSubmitBytes: composeSubmitBytes,
        composeIsSubmittable: composeIsSubmittable,
        composeBoxWanted: composeBoxWanted,
        paneTakesFocusOnOpen: paneTakesFocusOnOpen,
        composeLocked: composeLocked,
    };
})(typeof globalThis !== "undefined" ? globalThis : this);
