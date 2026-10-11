/*
 * The dashboard's rendering. Everything it draws is decided server-side —
 * which conversations hold columns, which machines are one machine, what a
 * failure means — so this file is presentation and polling and nothing else.
 *
 * The rule it enforces on its own is the one about absence: a figure that
 * is unknown is written as unknown. A bar drawn at zero for a conversation
 * whose count never arrived reads as room to spare, and a machine drawn at
 * 0% because its probe timed out reads as an idle machine.
 */
(function () {
  "use strict";

  const SNAPSHOT_MS = 5000;
  const TEXT_MS = 1000;
  // Bounded per column: this page is meant to be left open for days, and an
  // unbounded transcript in the DOM is how it becomes unusable by Thursday.
  const MAX_LINES = 300;
  // A socket that accepts and never answers rejects nothing, so without a
  // deadline one hung column pins the in-flight guard and silences every
  // column's text for good while the rest of the page keeps updating.
  const FETCH_TIMEOUT_MS = 8000;
  const STREAM_BATCH = 6;

  const columnsEl = document.getElementById("dash-columns");
  const providersEl = document.getElementById("dash-providers");
  const hostsEl = document.getElementById("dash-hosts");
  const idleEl = document.getElementById("dash-idle");
  const idleRowsEl = document.getElementById("dash-idle-rows");
  const idleEmptyEl = document.getElementById("dash-idle-empty");
  const errorEl = document.getElementById("dash-error");
  const observedEl = document.getElementById("dash-observed");

  /** Per session: the last sequence rendered, so each poll asks for the rest. */
  const cursors = new Map();

  const bytes = (n) => {
    if (n === null || n === undefined) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = n;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
  };

  const tokens = (n) =>
    n === null || n === undefined ? "unknown" : n.toLocaleString();

  const ago = (seconds) => {
    if (seconds === null || seconds === undefined) return "never";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
  };

  const GAUGE_R = 42;
  const GAUGE_C = 2 * Math.PI * GAUGE_R;

  function gauge(percent, band) {
    // The server drops any window whose figure is not a finite number, so
    // this should never see one — but a throw here escapes renderProviders
    // and takes the machines panel with it, leaving the last host reading
    // on screen forever with no staleness banner. Cheap insurance.
    if (typeof percent !== "number" || !Number.isFinite(percent)) return null;
    // A ring, not a bar: two windows sit side by side per provider and the
    // number belongs in the middle of the thing it describes. The band is
    // handed down by the server so the two thresholds live in one place.
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 100 100");
    svg.setAttribute("class", `dash-gauge is-${band || "unknown"}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `${percent.toFixed(0)} per cent used`);

    const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    track.setAttribute("class", "dash-gauge-track");
    track.setAttribute("cx", "50");
    track.setAttribute("cy", "50");
    track.setAttribute("r", String(GAUGE_R));
    svg.appendChild(track);

    const fill = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    fill.setAttribute("class", "dash-gauge-fill");
    fill.setAttribute("cx", "50");
    fill.setAttribute("cy", "50");
    fill.setAttribute("r", String(GAUGE_R));
    // Drawn from the top, clockwise, and clamped at full: past 100% the ring
    // cannot say more than "all of it", and the number beside it still does.
    const shown = Math.max(0, Math.min(percent, 100)) / 100;
    fill.setAttribute("stroke-dasharray", `${GAUGE_C}`);
    fill.setAttribute("stroke-dashoffset", `${GAUGE_C * (1 - shown)}`);
    fill.setAttribute("transform", "rotate(-90 50 50)");
    svg.appendChild(fill);

    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("class", "dash-gauge-text");
    text.setAttribute("x", "50");
    text.setAttribute("y", "50");
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("dominant-baseline", "central");
    text.textContent = `${percent.toFixed(0)}%`;
    svg.appendChild(text);
    return svg;
  }

  function contextBar(fraction, markFraction, over, markBeyondWindow) {
    // Vertical: it stands beside the prose rather than above it, so the
    // column reads as a tank filling while the mind talks. Height, not
    // width, and the rotation point is a line drawn across it.
    // Scaled to the model's whole context window, with the rotation point
    // marked on the same track. Two fractions of one bar: how full it is,
    // and where it turns over. A mark that defaulted to zero would claim
    // rotation fires immediately, so an absent one is simply not drawn.
    const wrap = document.createElement("div");
    wrap.className = "dash-meter dash-context-bar";
    const fill = document.createElement("div");
    fill.className = "dash-meter-fill";
    if (fraction === null || fraction === undefined) {
      wrap.classList.add("is-unknown");
    } else {
      fill.style.height = `${Math.min(fraction, 1) * 100}%`;
      // Decided server-side, against whichever denominator it found.
      if (over) wrap.classList.add("is-over");
    }
    wrap.appendChild(fill);
    if (markFraction !== null && markFraction !== undefined) {
      const mark = document.createElement("span");
      mark.className = "dash-meter-mark";
      mark.style.bottom = `${Math.min(markFraction, 1) * 100}%`;
      if (markBeyondWindow) {
        // Sat flat on the edge it reads as "rotates right at the end",
        // which is the reassuring version of a dropped context pin.
        mark.classList.add("is-beyond");
        mark.title = "rotation point is past the end of this window";
      } else {
        mark.title = "rotation point";
      }
      wrap.appendChild(mark);
    }
    return wrap;
  }

  function contextGauge(context) {
    const box = document.createElement("div");
    box.className = "dash-context-gauge";
    box.appendChild(
      contextBar(
        context.bar_fraction,
        context.mark_fraction,
        context.over,
        context.mark_beyond_window
      )
    );
    const pct = document.createElement("div");
    pct.className = "dash-context-pct";
    pct.textContent =
      context.bar_fraction === null || context.bar_fraction === undefined
        ? "—"
        : `${Math.round(context.bar_fraction * 100)}%`;
    if (context.over) pct.classList.add("is-over");
    box.appendChild(pct);
    return box;
  }

  function contextLines(context) {
    const box = document.createElement("div");
    box.className = "dash-context";

    const line = document.createElement("div");
    line.className = "dash-context-line";
    if (context.status === "unknown") {
      line.textContent = "no count reported by this mind";
      line.classList.add("is-unknown");
    } else {
      const threshold =
        context.threshold === null || context.threshold === undefined
          ? "no rotation point reported"
          : `rotates at ${tokens(context.threshold)}`;
      const denominator =
        context.window === null || context.window === undefined
          ? "window unknown"
          : `of ${tokens(context.window)}`;
      line.textContent = `${tokens(context.tokens)} ${denominator} · ${threshold} · measured ${ago(
        context.age_seconds
      )}`;
      if (context.kind === "estimated") line.textContent += " · estimated";
      if (context.bar_basis === "threshold") {
        line.textContent += " · bar scaled to the rotation point (window unknown)";
      }
      if (context.over) line.classList.add("is-over");
    }
    box.appendChild(line);
    return box;
  }

  function columnFor(session) {
    const column = document.createElement("article");
    column.className = "surface dash-column";
    column.dataset.session = session.session_id;

    const head = document.createElement("header");
    head.className = "dash-column-head";
    const name = document.createElement("h3");
    name.textContent = session.mind_name || session.mind_id || "unknown mind";
    head.appendChild(name);
    const meta = document.createElement("span");
    meta.className = "dash-column-meta";
    meta.textContent = session.model || "";
    head.appendChild(meta);
    column.appendChild(head);
    column.appendChild(contextLines(session.context || {}));

    // The bar stands to the left of the prose, full height of it: a tank
    // filling while the mind talks, rather than a rule above the text.
    const body = document.createElement("div");
    body.className = "dash-column-body";
    body.appendChild(contextGauge(session.context || {}));

    const stream = document.createElement("div");
    stream.className = "dash-stream";
    stream.dataset.stream = session.session_id;
    body.appendChild(stream);
    column.appendChild(body);
    return column;
  }

  function renderColumns(payload) {
    const shown = payload.columns || [];

    idleEl.hidden = shown.length > 0;

    const wanted = new Set(shown.map((row) => row.session_id));
    Array.from(columnsEl.children).forEach((child) => {
      if (!wanted.has(child.dataset.session)) {
        // The cursor goes with it. A column comes and goes constantly now
        // that a quiet conversation loses its column, and the stream
        // element is destroyed along with it — so a cursor kept at block
        // 24 brings the column back empty and refills it one new block at
        // a time, while a page refresh shows the lot. That is the "I have
        // to reload to see anything" bug: there is no existing stream left
        // to double-append into.
        cursors.delete(child.dataset.session);
        child.remove();
      }
    });

    shown.forEach((session) => {
      let column = columnsEl.querySelector(
        `[data-session="${CSS.escape(session.session_id)}"]`
      );
      if (!column) {
        column = columnFor(session);
        columnsEl.appendChild(column);
      } else {
        const fresh = columnFor(session);
        column
          .querySelector(".dash-column-head")
          .replaceWith(fresh.querySelector(".dash-column-head"));
        column
          .querySelector(".dash-context")
          .replaceWith(contextLines(session.context || {}));
        column
          .querySelector(".dash-context-gauge")
          .replaceWith(contextGauge(session.context || {}));
      }
    });
  }

  function renderIdle(rows) {
    idleRowsEl.replaceChildren();
    idleEmptyEl.hidden = rows.length > 0;
    rows.forEach((session) => {
      const context = session.context || {};
      const row = document.createElement("tr");
      const size =
        context.tokens === null || context.tokens === undefined
          ? "unknown"
          : tokens(context.tokens) +
            (context.kind === "estimated" ? " (estimated)" : "") +
            (context.over ? " (over)" : "");
      [
        session.mind_name || session.mind_id || "unknown mind",
        session.name || session.summary || session.session_id,
        session.model || "",
        size,
        tokens(context.threshold),
        tokens(context.window),
        ago(context.age_seconds),
      ].forEach((text, index) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        if (index === 3 && context.over) cell.classList.add("is-over");
        row.appendChild(cell);
      });
      idleRowsEl.appendChild(row);
    });
  }

  function renderProviders(payload) {
    providersEl.replaceChildren();
    const rows = (payload && payload.providers) || [];
    if (payload && payload.error) {
      // The note, then whatever the last good reading held — the same
      // shape the machines panel uses. Returning here instead threw away
      // every gauge on one timed-out refresh, which reads as a proxy that
      // was never configured.
      const note = document.createElement("p");
      note.className = "dash-empty is-error";
      note.textContent = rows.length
        ? `Allowance not refreshing: ${payload.error} — showing the last reading.`
        : `Allowance unavailable: ${payload.error}`;
      providersEl.appendChild(note);
    }
    rows.forEach((provider) => {
      const card = document.createElement("article");
      card.className = "metric surface";
      const label = document.createElement("div");
      label.className = "metric-label";
      label.textContent = provider.label || provider.name;
      card.appendChild(label);

      if (!provider.reporting) {
        const unknown = document.createElement("div");
        unknown.className = "metric-detail is-unknown";
        // Never a zero bar. "Nothing observed" and "nothing spent" are
        // opposite claims and only one of them means there is headroom.
        unknown.textContent = "no allowance observed yet";
        card.appendChild(unknown);
        providersEl.appendChild(card);
        return;
      }

      const gauges = document.createElement("div");
      gauges.className = "dash-gauge-row";
      (provider.windows || []).forEach((window) => {
        const cell = document.createElement("div");
        cell.className = "dash-provider-window";
        const ring = gauge(window.used_percent, window.band);
        if (!ring) return;
        cell.appendChild(ring);
        const heading = document.createElement("div");
        heading.className = "metric-detail";
        heading.textContent = window.label;
        cell.appendChild(heading);
        gauges.appendChild(cell);
      });
      card.appendChild(gauges);

      const age = document.createElement("div");
      age.className = "metric-detail";
      age.textContent = provider.stale
        ? `last seen ${ago(provider.age_seconds)} — may be out of date`
        : `as of ${ago(provider.age_seconds)}`;
      card.appendChild(age);
      providersEl.appendChild(card);
    });
  }

  function gpuLine(gpu) {
    const line = document.createElement("div");
    line.className = "metric-detail";
    if (!gpu) {
      line.textContent = "GPU not reported";
      return line;
    }
    if (gpu.status === "absent") {
      line.textContent = "no GPU in this machine";
      return line;
    }
    if (gpu.status === "tool_missing") {
      // Not "no GPU": the absence of nvidia-smi says nothing about the
      // hardware, and a Windows box or an AMD card lands here.
      line.textContent = "GPU status unknown (no probe tool)";
      line.classList.add("is-unknown");
      return line;
    }
    if (gpu.status === "query_failed") {
      line.textContent = `GPU probe failed: ${gpu.error || "unknown error"}`;
      line.classList.add("is-error");
      return line;
    }
    line.textContent = (gpu.devices || [])
      .map(
        (d) =>
          `${d.name} · ${d.utilization_percent}% · ${d.memory_used_mb}/${d.memory_total_mb} MB`
      )
      .join(" | ");
    return line;
  }

  function dialCell(label, percent, band, caption) {
    const cell = document.createElement("div");
    cell.className = "dash-dial";
    const ring = gauge(percent, band);
    if (!ring) return null;
    cell.appendChild(ring);
    const name = document.createElement("div");
    name.className = "dash-dial-label";
    name.textContent = label;
    cell.appendChild(name);
    if (caption) {
      const sub = document.createElement("div");
      sub.className = "dash-dial-caption";
      sub.textContent = caption;
      cell.appendChild(sub);
    }
    return cell;
  }

  function figureDial(label, figure, suffix) {
    // No dial when there is nothing measured: an empty ring reads as a
    // machine with everything free, which is the opposite of unknown.
    if (!figure || figure.usage_percent === undefined) return null;
    return dialCell(
      label,
      figure.usage_percent,
      figure.band,
      `${bytes(figure.used_bytes)} of ${bytes(figure.total_bytes)}${suffix || ""}`
    );
  }

  function gpuDials(gpu) {
    if (!gpu || gpu.status !== "reporting") return [];
    return (gpu.devices || [])
      .map((d) =>
        d.utilization_percent === undefined || d.utilization_percent === null
          ? null
          : dialCell(
              d.name || "GPU",
              d.utilization_percent,
              d.band,
              `${d.memory_used_mb}/${d.memory_total_mb} MB`
            )
      )
      .filter(Boolean);
  }

  function figureLine(label, figure, suffix) {
    const line = document.createElement("div");
    line.className = "metric-detail";
    if (!figure || figure.status !== "reporting") {
      line.textContent = `${label} unavailable`;
      line.classList.add("is-unknown");
      return line;
    }
    line.textContent = `${label} ${bytes(figure.used_bytes)} of ${bytes(
      figure.total_bytes
    )}${suffix || ""}`;
    return line;
  }

  function renderHosts(payload) {
    hostsEl.replaceChildren();
    const rows = (payload && payload.hosts) || [];
    if (payload && payload.stale) {
      // Requirement 27. The numbers below are the last ones that arrived,
      // and a refresh has failed since. Leaving them unlabelled is how a
      // page lies quietly all morning.
      const note = document.createElement("p");
      note.className = "dash-empty is-error";
      note.textContent = `Machine figures are not refreshing${
        payload.error ? `: ${payload.error}` : ""
      } — showing the last reading.`;
      hostsEl.appendChild(note);
    }
    if (!rows.length) {
      const note = document.createElement("p");
      note.className = "dash-empty";
      note.textContent = payload && payload.error
        ? `Machines unavailable: ${payload.error}`
        : "No machine has reported.";
      hostsEl.appendChild(note);
      return;
    }
    rows.forEach((host) => {
      const card = document.createElement("article");
      card.className = "metric surface";
      const label = document.createElement("div");
      label.className = "metric-label";
      label.textContent = host.hostname;
      card.appendChild(label);

      if (host.status !== "reporting") {
        const state = document.createElement("div");
        state.className = "metric-detail is-error";
        state.textContent = {
          refused: "refused the console's credential",
          unsupported: "running a build without the host route",
          unreachable: "unreachable",
          unconfigured: "no gateway URL registered",
        }[host.status] || host.status;
        card.appendChild(state);
        hostsEl.appendChild(card);
        return;
      }

      // Dials for what was measured; the probe's own absences stay as
      // sentences, because "no GPU", "no probe tool" and "probe failed"
      // are three different facts and a ring is none of them.
      const dials = document.createElement("div");
      dials.className = "dash-dial-row";
      gpuDials(host.gpu).forEach((d) => dials.appendChild(d));
      const memory = figureDial("Memory", host.memory);
      if (memory) dials.appendChild(memory);
      const disk = figureDial(
        "Disk",
        host.disk,
        host.disk && host.disk.reflects_host === false ? " (container)" : ""
      );
      if (disk) dials.appendChild(disk);
      if (dials.childElementCount) card.appendChild(dials);

      if (!host.gpu || host.gpu.status !== "reporting") {
        card.appendChild(gpuLine(host.gpu));
      }
      if (!memory) card.appendChild(figureLine("Memory", host.memory));
      if (!disk) {
        card.appendChild(
          figureLine(
            "Disk",
            host.disk,
            host.disk && host.disk.reflects_host === false
              ? " (container filesystem, not the host)"
              : ""
          )
        );
      }
      hostsEl.appendChild(card);
    });
  }

  let snapshotInFlight = false;
  let textInFlight = false;

  async function pollSnapshot() {
    // A slow poll must not be joined by the next one: two in flight both
    // render, and on the text path both read the same cursor and append the
    // same blocks, which reads as the model repeating itself.
    if (snapshotInFlight) return;
    snapshotInFlight = true;
    try {
      const response = await fetch("/api/dashboard", {
        credentials: "same-origin",
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!response.ok) throw new Error(`dashboard responded ${response.status}`);
      const payload = await response.json();
      // A 200 whose sessions half failed is the dangerous case: the page
      // would render "nothing is producing", which is what a quiet hive
      // looks like. Say which it is.
      if (payload.sessions_error) {
        errorEl.hidden = false;
        errorEl.textContent = `Conversations unavailable: ${payload.sessions_error}`;
      } else {
        errorEl.hidden = true;
      }
      renderColumns(payload);
      renderIdle(payload.idle || []);
      renderProviders(payload.providers);
      renderHosts(payload.hosts);
      observedEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      // Say so rather than leaving the last numbers standing as current —
      // a page that quietly stopped refreshing looks exactly like a quiet
      // hive, which is the failure that goes unnoticed for a morning.
      errorEl.hidden = false;
      errorEl.textContent = `Dashboard not refreshing: ${err.message}`;
      observedEl.textContent = "stale";
    } finally {
      snapshotInFlight = false;
    }
  }

  async function pollText() {
    if (textInFlight) return;
    textInFlight = true;
    try {
      await readAllStreams();
    } finally {
      textInFlight = false;
    }
  }

  async function readAllStreams() {
    const streams = Array.from(columnsEl.querySelectorAll("[data-stream]"));
    // There is no cap on columns any more, and each one is a relay the
    // console serves off a bounded thread pool shared with every other
    // route. Forty at once from one tab starves the rest of the console,
    // so they go in batches rather than in one fan-out.
    for (let i = 0; i < streams.length; i += STREAM_BATCH) {
      await readStreamBatch(streams.slice(i, i + STREAM_BATCH));
    }
  }

  async function readStreamBatch(streams) {
    await Promise.all(
      streams.map(async (stream) => {
        const id = stream.dataset.stream;
        const since = cursors.get(id) || 0;
        try {
          const response = await fetch(
            `/api/dashboard/text?session_id=${encodeURIComponent(id)}&since=${since}`,
            { credentials: "same-origin", signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) }
          );
          if (!response.ok) return;
          const payload = await response.json();

          // The buffer starts again from zero when the conversation behind
          // this session is replaced — a terminal rotation keeps the
          // session id — and when comms itself restarts. A cursor left at
          // 412 then asks for blocks past the end of a buffer holding 3,
          // and the column goes silent for good with nothing on screen
          // saying why. A latest_seq behind our cursor is that, and the
          // only honest response is to read the new conversation from its
          // start.
          if (payload.latest_seq !== undefined && payload.latest_seq < since) {
            cursors.set(id, 0);
            stream.replaceChildren();
            return;
          }

          // A reader that fell behind the buffer has missed a run. The
          // sequence numbers make that detectable, and it is drawn as a
          // break — joining the two halves would read as fluent prose with
          // its middle silently gone.
          if (since && payload.first_available_seq > since + 1) {
            const gap = document.createElement("div");
            gap.className = "dash-gap";
            gap.textContent = "— output missed —";
            stream.appendChild(gap);
          }

          (payload.blocks || []).forEach((block) => {
            // A command, a file and a sentence are three different things
            // to read. Rendering them as one undifferentiated run of prose
            // is how a column of tool output reads as the mind talking.
            const kind = block.kind || "text";
            const line = document.createElement("p");
            line.className = `dash-line dash-line-${kind}`;
            if (block.agent) line.classList.add("is-delegated");

            if (kind !== "text") {
              const tag = document.createElement("span");
              tag.className = "dash-line-tag";
              tag.textContent = block.agent
                ? `${block.agent} · ${block.name || kind}`
                : block.name || kind;
              line.appendChild(tag);
            } else if (block.agent) {
              const tag = document.createElement("span");
              tag.className = "dash-line-tag";
              tag.textContent = block.agent;
              line.appendChild(tag);
            }

            const body = document.createElement("span");
            body.className = "dash-line-body";
            body.textContent = block.text;
            line.appendChild(body);

            if (block.trimmed) {
              // Said rather than done silently: a reader who cannot tell a
              // truncation from a short result reads the wrong conclusion
              // off the screen.
              const cut = document.createElement("span");
              cut.className = "dash-line-trimmed";
              cut.textContent = "… earlier output trimmed";
              line.appendChild(cut);
            }

            stream.appendChild(line);
            cursors.set(id, block.seq);
          });
          while (stream.childElementCount > MAX_LINES) {
            stream.firstElementChild.remove();
          }
          stream.scrollTop = stream.scrollHeight;
        } catch (err) {
          /* one column's text is not the page */
        }
      })
    );
  }

  pollSnapshot();
  pollText();
  setInterval(pollSnapshot, SNAPSHOT_MS);
  setInterval(pollText, TEXT_MS);
})();
