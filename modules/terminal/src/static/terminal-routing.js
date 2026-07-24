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
     * Where a page of scrolling has to be done: here, or by the program.
     *
     * Claude's TUI takes the alternate screen buffer the moment it starts
     * (ESC[?1049h) and turns on SGR mouse reporting. An alternate buffer
     * has no scrollback by definition — xterm's own scrollLines has
     * nothing to move and the viewport has no overflow to drag — so the
     * conversation history the reader wants is inside the program's own
     * scroll region, and only the program can move it. Sending the page
     * keys down the wire is the fix, not the hazard: acting on them is
     * precisely what is wanted. On the normal buffer the tile owns the
     * scrollback and scrolls it locally, without disturbing the program.
     *
     * @param {Object} state  {altBuffer: boolean, rows: number, dir: -1|1}
     * @returns {{kind: "bytes", data: string}|{kind: "lines", lines: number}}
     */
    function pageScrollAction(state) {
        var dir = Number(state && state.dir) < 0 ? -1 : 1;
        if (state && state.altBuffer) {
            return {kind: "bytes", data: dir < 0 ? "\x1b[5~" : "\x1b[6~"};
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

    root.TerminalRouting = {
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
        attemptsAfterSocket: attemptsAfterSocket,
        attachExhausted: attachExhausted,
        contrastText: contrastText,
        pendingImeText: pendingImeText,
        shouldResetImeAccumulator: shouldResetImeAccumulator,
    };
})(typeof globalThis !== "undefined" ? globalThis : this);
