(function () {
  const stateLine = document.getElementById("catalogue-state");
  const figuresLine = document.getElementById("catalogue-figures");
  const errorBox = document.getElementById("catalogue-error");
  const table = document.getElementById("catalogue-table");
  if (!table) return;

  const DISPOSITIONS = ["escalates", "notifies", "record_only"];
  // Every bucket a promotion may name. Filled from the catalogue read, so a
  // bucket added later needs no change here.
  let slugs = [];

  function showError(message) {
    errorBox.textContent = message || "";
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
      const error = new Error(body.detail || body.error || `${response.status}`);
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  }

  function cell(row, text) {
    const td = document.createElement("td");
    td.textContent = text === null || text === undefined ? "" : String(text);
    row.appendChild(td);
    return td;
  }

  function textarea(value) {
    const area = document.createElement("textarea");
    area.value = value || "";
    area.rows = 6;
    area.cols = 54;
    return area;
  }

  function select(options, value) {
    const node = document.createElement("select");
    options.forEach((option) => {
      const item = document.createElement("option");
      item.value = option === null ? "" : option;
      item.textContent = option === null ? "does not promote" : option;
      if ((option === null ? "" : option) === (value === null || value === undefined ? "" : value)) {
        item.selected = true;
      }
      node.appendChild(item);
    });
    return node;
  }

  function number(value) {
    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.size = 6;
    input.value = value === null || value === undefined ? "" : String(value);
    return input;
  }

  function text(value) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    return input;
  }

  function wholeNumberOrNull(input) {
    const raw = input.value.trim();
    if (raw === "") return null;
    const parsed = Number(raw);
    if (!Number.isInteger(parsed)) return NaN;
    return parsed;
  }

  function render(bucket) {
    const tr = document.createElement("tr");
    tr.dataset.slug = bucket.slug;
    tr.dataset.revision = String(bucket.revision);

    cell(tr, bucket.slug);

    const conditionCell = document.createElement("td");
    const condition = textarea(bucket.description);
    conditionCell.appendChild(condition);
    tr.appendChild(conditionCell);

    const dispositionCell = document.createElement("td");
    const disposition = select(DISPOSITIONS, bucket.disposition);
    dispositionCell.appendChild(disposition);
    tr.appendChild(dispositionCell);

    const qualifierCell = document.createElement("td");
    const qualifier = text(bucket.qualifier);
    qualifierCell.appendChild(qualifier);
    tr.appendChild(qualifierCell);

    // A bucket that does not promote says so, rather than showing three
    // blanks that read as a rule nobody filled in.
    const promotionCell = document.createElement("td");
    const above = number(bucket.promotes_above);
    const into = select([null].concat(slugs), bucket.promotes_into);
    const window = number(bucket.promotion_window_minutes);
    const note = document.createElement("div");
    note.textContent = bucket.promotes ? "above" : "does not promote";
    promotionCell.appendChild(note);
    promotionCell.appendChild(above);
    const inWords = document.createElement("span");
    inWords.textContent = " in ";
    promotionCell.appendChild(inWords);
    promotionCell.appendChild(window);
    const minutes = document.createElement("span");
    minutes.textContent = " minutes, becomes ";
    promotionCell.appendChild(minutes);
    promotionCell.appendChild(into);
    tr.appendChild(promotionCell);

    const factsCell = document.createElement("td");
    const facts = text((bucket.requires_facts || []).join(", "));
    factsCell.appendChild(facts);
    tr.appendChild(factsCell);

    // Named in words, never by colour.
    const status = cell(
      tr,
      bucket.unreachable
        ? `unreachable: nothing supplies ${(bucket.missing_facts || []).join(", ")}`
        : "reachable"
    );
    status.dataset.unreachable = bucket.unreachable ? "yes" : "no";

    cell(tr, bucket.events);

    const actionCell = document.createElement("td");
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "save";
    save.addEventListener("click", async () => {
      showError("");
      const thresholds = {
        promotes_above: wholeNumberOrNull(above),
        promotion_window_minutes: wholeNumberOrNull(window),
      };
      if (Number.isNaN(thresholds.promotes_above) || Number.isNaN(thresholds.promotion_window_minutes)) {
        showError("a promotion threshold and window are whole numbers of events and minutes");
        return;
      }
      const payload = {
        slug: bucket.slug,
        revision: Number(tr.dataset.revision),
        description: condition.value,
        disposition: disposition.value,
        qualifier: qualifier.value,
        promotes_into: into.value || null,
        requires_facts: facts.value
          .split(",")
          .map((name) => name.trim())
          .filter((name) => name.length > 0),
        ...thresholds,
      };
      save.disabled = true;
      try {
        const answer = await call("/api/sentinel/buckets/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        tr.dataset.revision = String(answer.bucket.revision);
        await load();
      } catch (err) {
        if (err.status === 409) {
          showError(
            `${bucket.slug} was changed by somebody else since this page loaded. ` +
              "Refresh and re-apply the edit; nothing was saved."
          );
        } else {
          showError(`${bucket.slug}: ${err.message}`);
        }
      } finally {
        save.disabled = false;
      }
    });
    actionCell.appendChild(save);
    tr.appendChild(actionCell);
    return tr;
  }

  function age(stamp) {
    if (!stamp) return "nothing has been taken in";
    const then = new Date(stamp.replace(" ", "T") + "Z");
    if (Number.isNaN(then.getTime())) return stamp;
    const days = Math.floor((Date.now() - then.getTime()) / 86400000);
    if (days <= 0) return `last intake today (${stamp})`;
    if (days === 1) return `last intake 1 day ago (${stamp})`;
    return `last intake ${days} days ago (${stamp})`;
  }

  async function load() {
    showError("");
    let payload;
    try {
      payload = await call("/api/sentinel/buckets");
    } catch (err) {
      // Each of these sends somebody to a different machine, so none of them
      // is reported as any of the others.
      const says = {
        501: "the sensor is running a build without the catalogue API; pull and restart sentinel-review-api on it",
        401: "the sensor refused the console's bearer token",
        503: "the sensor or its store is busy, or this console is not configured; nothing was read",
        502: "the sensor is unreachable",
      };
      stateLine.dataset.state = "error";
      stateLine.textContent = says[err.status] || err.message;
      document.querySelector("#catalogue-table tbody").replaceChildren();
      return;
    }

    const buckets = payload.buckets || [];
    slugs = buckets.map((b) => b.slug);
    stateLine.dataset.state = buckets.length ? "ready" : "reachable_with_nothing";
    stateLine.textContent = buckets.length
      ? `serving build ${payload.build}`
      : "the sensor answered, and holds no bucket catalogue yet";

    const remainder = payload.remainder || {};
    const outside =
      (remainder.on_retired_classes || 0) +
      (remainder.on_classes_outside_the_nine || 0) +
      (remainder.on_no_class || 0);
    figuresLine.textContent =
      `${payload.total_events} events stored, ` +
      `${payload.total_events - outside} in the nine buckets below, ` +
      `${remainder.on_retired_classes || 0} on retired classes, ` +
      `${remainder.on_classes_outside_the_nine || 0} on classes outside the nine, ` +
      `${remainder.on_no_class || 0} on no class. ${age(payload.last_intake)}.`;

    const body = document.querySelector("#catalogue-table tbody");
    body.replaceChildren();
    buckets.forEach((bucket) => body.appendChild(render(bucket)));
  }

  const refresh = document.getElementById("catalogue-refresh");
  if (refresh) refresh.addEventListener("click", load);

  if (stateLine.dataset.state === "ready" || stateLine.dataset.state === "reachable_with_nothing") {
    load();
  }
})();
