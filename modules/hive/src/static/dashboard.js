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

  const columnsEl = document.getElementById("dash-columns");
  const providersEl = document.getElementById("dash-providers");
  const hostsEl = document.getElementById("dash-hosts");
  const waitingEl = document.getElementById("dash-waiting");
  const idleEl = document.getElementById("dash-idle");
  const errorEl = document.getElementById("dash-error");
  const observedEl = document.getElementById("dash-observed");
  const idleMindsEl = document.getElementById("dash-idle-minds");

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

  function meter(fraction, extraClass) {
    const wrap = document.createElement("div");
    wrap.className = `dash-meter ${extraClass || ""}`.trim();
    const fill = document.createElement("div");
    fill.className = "dash-meter-fill";
    if (fraction === null || fraction === undefined) {
      // Not a zero-width bar: an empty track with no fill at all, so an
      // absent measurement cannot be mistaken for a low one.
      wrap.classList.add("is-unknown");
    } else {
      fill.style.width = `${Math.min(fraction, 1) * 100}%`;
      if (fraction > 1) wrap.classList.add("is-over");
    }
    wrap.appendChild(fill);
    return wrap;
  }

  function contextBlock(context) {
    const box = document.createElement("div");
    box.className = "dash-context";

    const windowLine = document.createElement("div");
    windowLine.className = "dash-context-window";
    windowLine.textContent =
      context.window === null || context.window === undefined
        ? "context window unknown"
        : `${tokens(context.window)} context window`;
    box.appendChild(windowLine);

    box.appendChild(meter(context.fraction_of_threshold));

    const line = document.createElement("div");
    line.className = "dash-context-line";
    if (context.status === "unknown") {
      line.textContent = "no count reported by this mind";
      line.classList.add("is-unknown");
    } else {
      const threshold =
        context.threshold === null || context.threshold === undefined
          ? "no rotation point reported"
          : `of ${tokens(context.threshold)} before rotation`;
      line.textContent = `${tokens(context.tokens)} ${threshold} · measured ${ago(
        context.age_seconds
      )}`;
      if (context.over_threshold) line.classList.add("is-over");
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
    name.textContent = session.mind_id || "unknown mind";
    head.appendChild(name);
    const meta = document.createElement("span");
    meta.className = "dash-column-meta";
    meta.textContent = session.model || "";
    head.appendChild(meta);
    if (session.quiet) {
      const quiet = document.createElement("span");
      quiet.className = "dash-quiet";
      // Said out loud rather than shown by removing the column: silence
      // inside a long tool chain is the turn working, and a column that
      // vanished would say it had finished.
      quiet.textContent = "working quietly";
      head.appendChild(quiet);
    }
    column.appendChild(head);
    column.appendChild(contextBlock(session.context || {}));

    const stream = document.createElement("div");
    stream.className = "dash-stream";
    stream.dataset.stream = session.session_id;
    column.appendChild(stream);
    return column;
  }

  function renderColumns(payload) {
    const shown = payload.columns.shown || [];
    const waiting = payload.columns.waiting || [];

    idleEl.hidden = shown.length > 0;
    renderIdle(payload.idle_minds || []);
    waitingEl.textContent = waiting.length
      ? `${waiting.length} more generating, waiting for a column: ${waiting
          .map((row) => row.mind_id || row.session_id)
          .join(", ")}`
      : "";

    const wanted = new Set(shown.map((row) => row.session_id));
    Array.from(columnsEl.children).forEach((child) => {
      if (!wanted.has(child.dataset.session)) {
        // The cursor is kept. A chat conversation stops generating between
        // turns, so its column comes and goes every turn — dropping the
        // cursor makes the next one ask from zero and re-render the whole
        // retained buffer as if the mind had said it all again.
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
        const head = column.querySelector(".dash-column-head");
        const context = column.querySelector(".dash-context");
        head.replaceWith(columnFor(session).querySelector(".dash-column-head"));
        context.replaceWith(contextBlock(session.context || {}));
      }
    });
  }

  function renderIdle(minds) {
    // Requirement 5. Without this a quiet hive and a hive whose minds have
    // all gone render identically, and the page cannot tell you which.
    if (!idleMindsEl) return;
    idleMindsEl.textContent = minds.length
      ? `Connected and idle: ${minds.map((m) => m.name || m.mind_id).join(", ")}`
      : "";
  }

  function renderProviders(payload) {
    providersEl.replaceChildren();
    const rows = (payload && payload.providers) || [];
    if (payload && payload.error) {
      const note = document.createElement("p");
      note.className = "dash-empty";
      note.textContent = `Allowance unavailable: ${payload.error}`;
      providersEl.appendChild(note);
      return;
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

      (provider.windows || []).forEach((window) => {
        const row = document.createElement("div");
        row.className = "dash-provider-window";
        const heading = document.createElement("div");
        heading.className = "metric-detail";
        heading.textContent = `${window.label} · ${window.used_percent.toFixed(0)}% used`;
        row.appendChild(heading);
        row.appendChild(meter(window.used_percent / 100));
        card.appendChild(row);
      });

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

      const minds = document.createElement("div");
      minds.className = "metric-detail";
      minds.textContent = (host.minds || []).filter(Boolean).join(", ");
      card.appendChild(minds);

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

      card.appendChild(gpuLine(host.gpu));
      card.appendChild(figureLine("Memory", host.memory));
      if (host.observed_at) {
        const when = document.createElement("div");
        when.className = "metric-detail";
        when.textContent = `read ${ago(Date.now() / 1000 - host.observed_at)}`;
        card.appendChild(when);
      }
      card.appendChild(
        figureLine(
          "Disk",
          host.disk,
          host.disk && host.disk.reflects_host === false
            ? " (container filesystem, not the host)"
            : ""
        )
      );
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
      const response = await fetch("/api/dashboard", { credentials: "same-origin" });
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
    await Promise.all(
      streams.map(async (stream) => {
        const id = stream.dataset.stream;
        const since = cursors.get(id) || 0;
        try {
          const response = await fetch(
            `/api/dashboard/text?session_id=${encodeURIComponent(id)}&since=${since}`,
            { credentials: "same-origin" }
          );
          if (!response.ok) return;
          const payload = await response.json();

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
            const line = document.createElement("p");
            line.className = "dash-line";
            line.textContent = block.text;
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
