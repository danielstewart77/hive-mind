(function () {
    function escapeHtml(value) {
        return String(value == null ? "" : value).replace(/[&<>'"]/g, function (char) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"}[char];
        });
    }

    async function requestJson(url, options) {
        const response = await fetch(url, options);
        let body = {};
        try { body = await response.json(); } catch (error) { body = {}; }
        if (!response.ok) throw new Error(body.detail || ("Request failed with status " + response.status));
        return body;
    }

    function formatTime(value) {
        if (!value) return "No recent activity";
        let date;
        if (typeof value === "number") date = new Date(value < 1000000000000 ? value * 1000 : value);
        else date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
    }

    function statusPill(ok, goodText, badText) {
        return '<span class="status-pill ' + (ok ? "is-good" : "is-bad") + '"><span></span>' +
            escapeHtml(ok ? goodText : badText) + "</span>";
    }

    function serviceCheckRow(name, description, service, icon, options) {
        options = options || {};
        service = service || {};
        const optional = options.optional === true;
        const goodText = options.goodText || "Ready";
        const badText = optional ? "Optional" : (options.badText || "Needs attention");
        const state = service.reachable ? goodText : badText;
        const message = service.message || (service.reachable ? "The health check passed." : String(service.detail || "The health check failed."));
        const action = service.action || (service.reachable ? "No action is required." : "Check the service configuration and retry.");
        let diagnostic = service.detail;
        if (diagnostic && typeof diagnostic === "object") {
            try { diagnostic = JSON.stringify(diagnostic); } catch (error) { diagnostic = String(diagnostic); }
        }
        diagnostic = String(diagnostic || (service.status_code ? "HTTP " + service.status_code : "No diagnostic detail was returned."));
        if (diagnostic.length > 300) diagnostic = diagnostic.slice(0, 297) + "…";
        const endpoint = service.endpoint ? '<div><dt>Endpoint</dt><dd class="mono">' + escapeHtml(service.endpoint) + '</dd></div>' : "";
        const retry = options.retry ? '<button class="button button-quiet" type="button" data-health-retry><i data-lucide="refresh-cw"></i>Retry check</button>' : "";
        return '<li class="service-item"><details class="service-disclosure"><summary class="service-row"><span class="service-icon"><i data-lucide="' + escapeHtml(icon) + '"></i></span><span class="service-copy"><strong>' + escapeHtml(name) + '</strong><small>' + escapeHtml(description) + '</small></span>' + statusPill(Boolean(service.reachable), goodText, badText) + '<i class="service-chevron" data-lucide="chevron-down"></i></summary><div class="service-diagnostic"><dl><div><dt>State</dt><dd>' + escapeHtml(state) + '</dd></div>' + endpoint + '<div><dt>Health check</dt><dd>' + escapeHtml(message) + '</dd></div><div><dt>Diagnostic</dt><dd class="mono">' + escapeHtml(diagnostic) + '</dd></div><div><dt>Next step</dt><dd>' + escapeHtml(action) + '</dd></div></dl>' + retry + '</div></details></li>';
    }

    function normalizeOverview(data) {
        const sessions = Array.isArray(data.sessions) ? data.sessions : [];
        const activeStatuses = new Set(["active", "running"]);
        const activity = new Map();
        sessions.forEach(function (session) {
            const mindId = String(session.mind_id || "");
            if (!mindId) return;
            const current = activity.get(mindId) || {lastActivity: null, activeCount: 0};
            const timestamp = Number(session.last_active || session.created_at || 0) || null;
            if (timestamp && (!current.lastActivity || timestamp > current.lastActivity)) current.lastActivity = timestamp;
            if (activeStatuses.has(session.status)) current.activeCount += 1;
            activity.set(mindId, current);
        });
        data.counts = data.counts || {};
        data.counts.sessions = sessions.filter(function (session) { return activeStatuses.has(session.status); }).length;
        data.minds = (data.minds || []).map(function (mind) {
            if (Object.prototype.hasOwnProperty.call(mind, "last_activity")) return mind;
            const current = activity.get(String(mind.id || "")) || activity.get(String(mind.name || ""));
            return Object.assign({}, mind, {
                last_activity: current ? current.lastActivity : null,
                active_session_count: current ? current.activeCount : 0,
                activity_status: current && current.activeCount ? "active" : "registered"
            });
        });
        if (!Array.isArray(data.edge_minds)) {
            data.edge_minds = data.minds.filter(function (mind) {
                const local = mind.local_config;
                const config = local && local.config || {};
                return !local || Boolean(config.remote || config.role || config.deployment);
            });
        }
        if (!Array.isArray(data.capability_nodes)) data.capability_nodes = [];
        if (data.counts.edge_minds == null) data.counts.edge_minds = data.edge_minds.length;
        if (data.counts.capability_nodes == null) {
            data.counts.capability_nodes = data.services && data.services.tools && data.services.tools.installed ? 1 : 0;
        }
        const comms = data.services && data.services.comms;
        if (comms && !comms.reachable && ((data.minds || []).length || sessions.length)) {
            data.services.comms = Object.assign({}, comms, {
                reachable: true,
                status_code: 200,
                message: "Hive Comms returned live registry and session data.",
                action: "No action is required."
            });
        }
        return data;
    }

    window.HiveConsole = {escapeHtml: escapeHtml, requestJson: requestJson, formatTime: formatTime, statusPill: statusPill, serviceCheckRow: serviceCheckRow, normalizeOverview: normalizeOverview};

    const app = document.querySelector("[data-console-app]");
    if (app) {
        document.querySelectorAll("[data-sidebar-open]").forEach(function (button) {
            button.addEventListener("click", function () { app.classList.add("sidebar-open"); });
        });
        document.querySelectorAll("[data-sidebar-close]").forEach(function (button) {
            button.addEventListener("click", function () { app.classList.remove("sidebar-open"); });
        });
    }

    document.addEventListener("click", async function (event) {
        const button = event.target.closest("[data-copy]");
        if (!button) return;
        const target = document.querySelector(button.dataset.copy);
        if (!target) return;
        await navigator.clipboard.writeText(target.textContent);
        const original = button.getAttribute("aria-label") || "Copy";
        button.setAttribute("aria-label", "Copied");
        button.classList.add("is-copied");
        window.setTimeout(function () {
            button.setAttribute("aria-label", original);
            button.classList.remove("is-copied");
        }, 1400);
    });

    if (window.lucide) window.lucide.createIcons({attrs: {"stroke-width": 1.8}});
})();
