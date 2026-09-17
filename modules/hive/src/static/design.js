/* Design sessions: the list, and the three pages of one session.

   Every write carries the revision the page was read at. A revision that is
   optional on the wire is not a check at all — the first caller to omit it
   overwrites whatever landed since — so a stale one comes back 409 and the
   page reloads rather than clobbering the other tab. */

const DESIGN_API = "/api/design";
const PAGES = ["requirements", "tests", "functions"];

function designEscape(value) {
    return String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function designWhen(seconds) {
    if (!seconds) { return "never"; }
    return new Date(seconds * 1000).toLocaleString();
}

function designShowError(message) {
    const box = document.getElementById("design-error");
    if (!box) { return; }
    box.textContent = message || "";
    box.style.display = message ? "" : "none";
}

async function designFetch(path, options) {
    const response = await fetch(DESIGN_API + path, options);
    if (!response.ok) {
        let detail = response.statusText;
        try { detail = (await response.json()).detail || detail; } catch (err) { /* body was not json */ }
        const error = new Error(detail);
        error.status = response.status;
        throw error;
    }
    return response.json();
}

function designList() {
    async function draw() {
        try {
            const data = await designFetch("/sessions");
            const body = document.getElementById("design-rows");
            const empty = document.getElementById("design-empty");
            body.innerHTML = "";
            empty.style.display = data.sessions.length ? "none" : "";
            data.sessions.forEach(function (row) {
                const counts = PAGES.map(function (page) { return row.counts[page]; }).join(" / ");
                const tr = document.createElement("tr");
                tr.innerHTML =
                    '<td><a href="/design/' + designEscape(row.id) + '">' + designEscape(row.title) + "</a></td>" +
                    "<td>" + (row.conversation_id
                        ? designEscape(row.surface || "conversation") + " · " + designEscape(row.conversation_id.slice(0, 8))
                        : "<em>nothing attached</em>") + "</td>" +
                    "<td>" + counts + "</td>" +
                    "<td>" + designEscape(designWhen(row.updated_at)) + "</td>" +
                    '<td><button type="button" data-delete="' + designEscape(row.id) + '">delete</button></td>';
                body.appendChild(tr);
            });
            body.querySelectorAll("[data-delete]").forEach(function (button) {
                button.addEventListener("click", async function () {
                    if (!window.confirm("Delete this design session? It cannot be undone.")) { return; }
                    try {
                        await designFetch("/sessions/" + button.dataset.delete, { method: "DELETE" });
                        draw();
                    } catch (err) { designShowError(err.message); }
                });
            });
            designShowError("");
        } catch (err) { designShowError(err.message); }
    }
    draw();
    setInterval(draw, 10000);
}

function designDetail() {
    const root = document.querySelector("[data-design-id]");
    const designId = root.dataset.designId;
    let page = "requirements";
    let record = null;
    let drawn = false;
    /* Everything typed and not yet submitted, keyed by page and item, kept
       across redraws and across tab switches. The poll rebuilds the DOM from
       the server record every few seconds; without this, a question longer
       than one typing burst cannot be finished, let alone sent. */
    const pending = { requirements: {}, tests: {}, functions: {} };

    function itemLabel(item) {
        if (page === "requirements") { return item.text || ""; }
        if (page === "tests") { return item.sentence || ""; }
        return item.name || "";
    }

    function symbolBlock(item) {
        const symbol = item.symbol || {};
        if (!symbol.name) { return ""; }
        const where = symbol.resolved
            ? designEscape(symbol.path) + " lines " + symbol.first_line + "–" + symbol.last_line
            : designEscape(symbol.note || "not resolved");
        const state = symbol.resolved ? "is-resolved" : (symbol.kind === "planned" ? "is-planned" : "is-unresolved");
        return '<div class="design-symbol ' + state + '">' +
            "<strong>" + designEscape(symbol.name) + "</strong> <span>" + where + "</span>" +
            (symbol.source ? "<pre>" + designEscape(symbol.source) + "</pre>" : "") +
            "</div>";
    }

    function casesBlock(item) {
        const cases = item.cases || [];
        if (!cases.length) { return ""; }
        const rows = cases.map(function (one) {
            return "<tr><td>" + designEscape(one.input) + "</td><td>" + designEscape(one.expected) +
                "</td><td>" + designEscape(one.outcome || "") + "</td></tr>";
        }).join("");
        return '<table class="design-cases"><thead><tr><th>given</th><th>expected</th><th>outcome</th></tr></thead><tbody>' +
            rows + "</tbody></table>";
    }

    function graphBlock(item) {
        const calls = (item.calls || item.callees || []).join(", ");
        const callers = (item.called_by || item.callers || []).join(", ");
        const bits = [];
        if (callers) { bits.push("called by " + designEscape(callers)); }
        if (calls) { bits.push("calls " + designEscape(calls)); }
        if (item.params) { bits.push("takes " + designEscape(item.params)); }
        if (item.args) { bits.push("passes " + designEscape(item.args)); }
        if (item.file) { bits.push(designEscape(item.file)); }
        return bits.length ? '<p class="design-graph">' + bits.join(" · ") + "</p>" : "";
    }

    function stash() {
        const host = document.getElementById("design-items");
        if (!host) { return; }
        host.querySelectorAll("[data-key]").forEach(function (field) {
            pending[page][field.dataset.key] = pending[page][field.dataset.key] || {};
            pending[page][field.dataset.key].text = field.value;
        });
        host.querySelectorAll("[data-feedback]").forEach(function (field) {
            const key = field.dataset.feedback;
            pending[page][key] = pending[page][key] || {};
            pending[page][key].feedback = field.value;
        });
    }

    function held(key, field, fallback) {
        const kept = pending[page][key];
        if (kept && kept[field] !== undefined) { return kept[field]; }
        return fallback;
    }

    /* Which box the caret is in, and where in it. drawItems replaces every
       textarea on the page, so without this a redraw lands the cursor on the
       body and Daniel loses his place mid-sentence — every five seconds. */
    function focusMark() {
        const field = document.activeElement;
        if (!field || !field.dataset) { return null; }
        const key = field.dataset.key || field.dataset.feedback;
        if (!key) { return null; }
        return {
            key: key,
            kind: field.dataset.key ? "key" : "feedback",
            start: field.selectionStart,
            end: field.selectionEnd,
            scroll: field.scrollTop,
        };
    }

    function focusRestore(mark) {
        if (!mark) { return; }
        const attr = mark.kind === "key" ? "data-key" : "data-feedback";
        const field = document.querySelector(
            '#design-items [' + attr + '="' + (window.CSS && CSS.escape ? CSS.escape(mark.key) : mark.key) + '"]'
        );
        if (!field) { return; }
        field.focus({ preventScroll: true });
        /* Selection before scroll: setting it scrolls the box itself. */
        try { field.setSelectionRange(mark.start, mark.end); } catch (e) {}
        field.scrollTop = mark.scroll;
    }

    function drawItems() {
        const host = document.getElementById("design-items");
        const items = record.pages[page];
        host.innerHTML = "";
        if (!items.length) {
            host.innerHTML = '<p class="event-hints">Nothing published to this page yet.</p>';
            drawn = true;
            return;
        }
        items.forEach(function (item, index) {
            const editable = page !== "functions";
            const block = document.createElement("div");
            block.className = "design-item" + (item.changed === false ? " is-context" : "");
            block.innerHTML =
                '<div class="design-item-head"><span class="design-number">' + (index + 1) + "</span>" +
                (item.role === "context" ? '<span class="design-tag">unchanged</span>' : "") + "</div>" +
                (editable
                    ? '<textarea class="design-text" data-key="' + designEscape(item.key) + '" rows="3">' +
                      designEscape(held(item.key, "text", itemLabel(item))) + "</textarea>"
                    : "<p class=\"design-name\">" + designEscape(itemLabel(item)) + "</p>") +
                symbolBlock(item) + casesBlock(item) + graphBlock(item) +
                (item.question_answered
                    ? '<p class="design-answered"><em>you asked:</em> ' + designEscape(item.question_answered) + "</p>"
                    : "") +
                (item.answer ? '<p class="design-answer">' + designEscape(item.answer) + "</p>" : "") +
                '<textarea class="design-feedback" data-feedback="' + designEscape(item.key) +
                '" rows="2" placeholder="question or correction">' +
                designEscape(held(item.key, "feedback", item.feedback || "")) + "</textarea>";
            host.appendChild(block);
        });
        host.querySelectorAll("textarea").forEach(function (field) {
            field.addEventListener("input", stash);
        });
        drawn = true;
    }

    function collect() {
        /* Read from the stash, not from the DOM: feedback typed on page one
           and submitted from page two is only in the stash, and the DOM only
           ever holds the page currently rendered. */
        stash();
        const items = {};
        Object.keys(pending[page]).forEach(function (key) {
            const kept = pending[page][key];
            items[key] = {};
            if (kept.text !== undefined) {
                items[key][page === "tests" ? "sentence" : "text"] = kept.text;
            }
            if (kept.feedback !== undefined) { items[key].feedback = kept.feedback; }
        });
        return items;
    }

    function drawChrome() {
        document.getElementById("design-title").textContent = record.title;
        const binding = document.getElementById("design-binding");
        binding.textContent = record.conversation_id
            ? "attached to " + (record.surface || "a conversation") + " · " + record.conversation_id
            : "not attached to any conversation — nothing can be sent until a mind claims it";
        document.querySelectorAll(".design-tab").forEach(function (tab) {
            tab.classList.toggle("is-active", tab.dataset.page === page);
        });
        const stale = document.getElementById("design-stale");
        const isStale = record.stale && record.stale[page];
        stale.style.display = isStale ? "" : "none";
        stale.textContent = isStale
            ? "This page was written against older requirements."
            : "";
        const gate = record.gates[page] || [false, ""];
        document.getElementById("design-gate").textContent = gate[0] ? "" : gate[1];
        const advance = document.getElementById("design-advance");
        advance.textContent = page === "functions" ? "build" : "accept";
        advance.disabled = !gate[0];
    }

    async function load() {
        try {
            stash();
            const next = await designFetch("/sessions/" + designId);
            const moved = record && next.revision !== record.revision;
            /* The record is always taken, even when it moved under a
               half-typed box: keeping a stale one is how a 409 becomes
               permanent, since every later write carries the old revision.
               What Daniel typed lives in the stash and survives the redraw. */
            record = next;
            const mark = focusMark();
            drawChrome();
            /* Only when the server record actually changed. A poll that found
               nothing new has no business rebuilding the DOM Daniel is typing
               into, and the redraw was costing him the caret on every tick. */
            if (moved || !drawn) { drawItems(); focusRestore(mark); }
            designShowError(
                moved
                    ? "This page was updated while you were typing. Your text is still here."
                    : ""
            );
        } catch (err) { designShowError(err.message); }
    }

    async function post(suffix, body) {
        try {
            await designFetch(
                "/sessions/" + designId + "/pages/" + page + "/" + suffix,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(Object.assign({ revision: record.revision }, body)),
                }
            );
            pending[page] = {};
            await load();
        } catch (err) {
            /* A conflict is recoverable: take the new record so the next
               attempt carries a live revision, and keep what was typed. */
            if (err.status === 409) { await load(); }
            designShowError(err.message);
        }
    }

    document.querySelectorAll(".design-tab").forEach(function (tab) {
        tab.addEventListener("click", function () {
            stash();
            page = tab.dataset.page;
            drawChrome();
            drawItems();
        });
    });
    document.getElementById("design-submit").addEventListener("click", function () {
        post("submit", { items: collect(), action: "feedback" });
    });
    document.getElementById("design-advance").addEventListener("click", function () {
        if (page === "functions") {
            post("submit", { items: collect(), action: "build" });
        } else {
            post("accept", {}).then(function () {
                /* Accept advances. Leaving Daniel on the page he just
                   accepted, with only the buttons flickering, reads as a
                   button that did nothing. */
                stash();
                page = page === "requirements" ? "tests" : "functions";
                drawChrome();
                drawItems();
            });
        }
    });

    load();
    setInterval(load, 5000);
}
