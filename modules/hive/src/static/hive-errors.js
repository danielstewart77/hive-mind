(function (root) {
    'use strict';
    function createErrorPoller(options) {
        let active = false, generation = 0, timer, controller;
        async function poll(run) {
            if (!active || run !== generation) return;
            const requestController = new AbortController();
            controller = requestController;
            const deadline = setTimeout(() => requestController.abort(), 15000);
            try {
                const data = await options.request(requestController.signal);
                if (active && run === generation) options.render(data);
            } catch (error) {
                if (active && run === generation) options.error(error);
            } finally {
                clearTimeout(deadline);
                if (active && run === generation) timer = options.schedule(() => poll(run), 5000);
            }
        }
        return {
            start() { if (!active) { active = true; poll(++generation); } },
            stop() { active = false; generation++; options.cancel(timer); if (controller) controller.abort(); },
        };
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = {createErrorPoller};
    if (!root.document) return;
    root.addEventListener('DOMContentLoaded', function () {
        const list = document.getElementById('hive-error-components');
        if (!list) return;
        const h = root.HiveConsole;
        const status = document.getElementById('hive-error-status');
        function render(data) {
            status.textContent = 'Last checked ' + new Date().toLocaleTimeString() + ' · Refreshes every 5 seconds';
            list.replaceChildren();
            (data.components || []).forEach(component => {
                const section = document.createElement('section'); section.className = 'surface';
                const header = document.createElement('div'); header.className = 'settings-section-copy';
                const title = document.createElement('h2'); title.textContent = component.name;
                const note = document.createElement('p');
                note.textContent = component.detail || ('Error feed available' + (component.started_at ? ' · Since ' + new Date(component.started_at).toLocaleString() : ''));
                header.append(title, note); section.append(header);
                const body = document.createElement('div'); body.className = 'surface-body';
                if (!component.errors || !component.errors.length) {
                    const message = document.createElement('p');
                    message.textContent = component.status === 'ok' ? 'No errors in the retained feed.' : component.status === 'health-only' ? 'Component is responding.' : 'Diagnostics incomplete: ' + component.status;
                    body.append(message);
                }
                (component.errors || []).forEach(item => {
                    const entry = document.createElement('div'); entry.className = 'error-state';
                    const heading = document.createElement('strong'); heading.textContent = (item.timestamp ? new Date(item.timestamp).toLocaleString() + ' · ' : '') + item.message;
                    const detail = document.createElement('p'); detail.textContent = item.detail || '';
                    entry.append(heading, detail); body.append(entry);
                });
                section.append(body); list.append(section);
            });
        }
        const poller = createErrorPoller({
            request: signal => h.requestJson('/api/hive-errors', {signal}), render,
            error: error => { status.textContent = 'Diagnostics request failed: ' + error.message + ' · Retrying in 5 seconds'; },
            schedule: root.setTimeout.bind(root), cancel: root.clearTimeout.bind(root),
        });
        root.addEventListener('pagehide', () => poller.stop());
        root.addEventListener('pageshow', () => { if (!document.hidden) poller.start(); });
        document.addEventListener('visibilitychange', () => document.hidden ? poller.stop() : poller.start());
        if (!document.hidden) poller.start();
    });
})(typeof window === 'undefined' ? globalThis : window);
