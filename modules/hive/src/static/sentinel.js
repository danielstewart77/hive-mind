(function () {
  const errorBox = document.getElementById("sentinel-error");
  const countLine = document.getElementById("sentinel-count");
  const sweepLine = document.getElementById("sentinel-sweep");

  function showError(message) {
    errorBox.textContent = message;
    errorBox.style.display = message ? "" : "none";
  }

  async function call(url, options) {
    const response = await fetch(url, options);
    let body = {};
    try {
      body = await response.json();
    } catch (err) {
      body = {};
    }
    if (!response.ok) {
      throw new Error(body.detail || body.error || `${response.status}`);
    }
    return body;
  }

  function cell(row, text) {
    const td = document.createElement("td");
    td.textContent = text === null || text === undefined ? "" : String(text);
    row.appendChild(td);
    return td;
  }

  function actionButton(label, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
  }

  function fill(tableId, rows, render) {
    const body = document.querySelector(`#${tableId} tbody`);
    body.replaceChildren();
    rows.forEach((item) => body.appendChild(render(item)));
  }

  async function load() {
    showError("");
    try {
      const [review, queue] = await Promise.all([
        call("/api/sentinel/review"),
        call("/api/sentinel/queue"),
      ]);

      fill("sentinel-queue", queue.classes || [], (row) => {
        const tr = document.createElement("tr");
        cell(tr, row.slug);
        cell(tr, row.bucket);
        cell(tr, row.n);
        cell(tr, row.rule_count);
        cell(tr, row.first_seen);
        cell(tr, row.last_seen);
        return tr;
      });

      fill("sentinel-rules", review.rules || [], (rule) => {
        const tr = document.createElement("tr");
        cell(tr, rule.class_slug);
        cell(tr, rule.name || "");
        cell(tr, rule.authorized_by || "");
        cell(tr, rule.created_at || "");
        cell(tr, rule.action_kind);
        cell(tr, rule.condition_expr || "always");
        cell(tr, rule.applied_count);
        cell(tr, rule.latest_summary || "");
        const actions = document.createElement("td");
        actions.appendChild(
          actionButton("looks right", async () => {
            await act(() =>
              call("/api/sentinel/review/mark", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ kind: "rule", id: rule.id }),
              })
            );
          })
        );
        actions.appendChild(
          actionButton("stop silencing", async () => {
            await act(() =>
              call("/api/sentinel/review/overturn", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rule_id: rule.id, retire: true }),
              })
            );
          })
        );
        actions.appendChild(
          actionButton("page me instead", async () => {
            await act(() =>
              call("/api/sentinel/review/overturn", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rule_id: rule.id, action_kind: "notify_daniel" }),
              })
            );
          })
        );
        tr.appendChild(actions);
        return tr;
      });

      fill("sentinel-classes", review.classes || [], (klass) => {
        const tr = document.createElement("tr");
        cell(tr, klass.slug);
        cell(tr, klass.bucket);
        cell(tr, klass.created_by || "");
        cell(tr, klass.created_at || "");
        cell(tr, klass.event_count);
        cell(tr, klass.latest_summary || "");
        const actions = document.createElement("td");
        actions.appendChild(
          actionButton("looks right", async () => {
            await act(() =>
              call("/api/sentinel/review/mark", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ kind: "class", id: klass.id }),
              })
            );
          })
        );
        tr.appendChild(actions);
        return tr;
      });

      const queued = (queue.classes || []).reduce((sum, row) => sum + (row.n || 0), 0);
      countLine.textContent =
        `${queued} events awaiting decision across ${(queue.classes || []).length} classes · ` +
        `${(review.rules || []).length} unreviewed rules · ${(review.classes || []).length} unreviewed classes`;
    } catch (err) {
      showError(err.message);
    }
  }

  async function act(fn) {
    showError("");
    try {
      await fn();
      await load();
    } catch (err) {
      showError(err.message);
    }
  }

  document.getElementById("sentinel-refresh").addEventListener("click", load);
  document.getElementById("sentinel-apply-queue").addEventListener("click", () => {
    if (!window.confirm(
      "Apply every approved record-only rule to the whole awaiting-decision queue? " +
      "This closes those events and cannot be undone."
    )) {
      return;
    }
    act(async () => {
      const result = await call("/api/sentinel/queue/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      sweepLine.textContent =
        `examined ${result.examined}, closed ${result.retired}, ` +
        `${result.left_for_side_effecting_rule} left for a rule that would notify, ` +
        `${result.left_for_rate_threshold} left as a rate-threshold burst`;
    });
  });

  load();
})();
