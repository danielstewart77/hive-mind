/* Account-scoped controls. No credential is stored in browser persistence. */
window.addEventListener("DOMContentLoaded", function () {
    const h = window.HiveConsole;
    const form = document.getElementById("remote-access-form");
    const buttons = Array.from(document.querySelectorAll("[data-remote-toggle]"));
    const labels = Array.from(document.querySelectorAll("[data-remote-status]"));
    const global = document.getElementById("remote-global-allowed");
    let state = null, busy = false, initialized = false;
    const json = body => ({headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    function paint(value) {
        state = value;
        const open = ["armed", "connecting"].includes(value.state);
        buttons.forEach(button => {
            button.textContent = open ? "Disarm remote access" : "Arm remote access";
            button.disabled = busy || (!open && (!value.allowed || !value.configured));
        });
        const deadline = value.closes_at ? new Date(value.closes_at).toLocaleString(undefined, {timeZone: "America/Chicago"}) + " Central" : "";
        labels.forEach(label => label.textContent = !value.allowed ? "Disabled globally by an admin" : open ? (value.state === "connecting" ? "Connecting…" : "Armed") + " · Closes " + deadline : value.configured ? "Disarmed" : "Configure your connection in Settings");
        if (form) {
            if (!initialized) { form.elements.website_url.value = value.website_url; form.elements.terminal_url.value = value.terminal_url; initialized = true; }
            form.querySelector('[type="submit"]').disabled = busy || open;
            form.elements.tunnel_token.required = !value.token_saved;
            document.getElementById("remote-token-help").textContent = value.token_saved ? "A token is saved. Leave blank to keep it, or paste a replacement while disarmed." : "Paste the token for this tunnel, not an account API key.";
        }
        if (global) { global.checked = value.allowed; global.disabled = busy; }
    }
    function failure(error) { labels.forEach(label => label.textContent = error.message); }
    async function refresh() { paint(await h.requestJson("/api/settings/remote-access")); }
    buttons.forEach(button => button.addEventListener("click", async function () {
        if (!state || busy) return;
        const action = ["armed", "connecting"].includes(state.state) ? "off" : "on";
        busy = true; paint(state);
        labels.forEach(label => label.textContent = action === "on" ? "Connecting and checking both addresses…" : "Closing remote access…");
        try { paint(await h.requestJson("/api/remote-access/" + action, {method: "POST"})); }
        catch (error) { failure(error); }
        finally { busy = false; if (state) { buttons.forEach(b => b.disabled = !["armed", "connecting"].includes(state.state) && (!state.allowed || !state.configured)); if (form) form.querySelector('[type="submit"]').disabled = ["armed", "connecting"].includes(state.state); if (global) global.disabled = false; } }
    }));
    if (form) form.addEventListener("submit", async function (event) {
        event.preventDefault(); if (busy) return;
        busy = true; if (state) paint(state);
        const status = document.getElementById("remote-save-status");
        const body = {}; ["website_url", "terminal_url", "tunnel_token"].forEach(key => body[key] = form.elements[key].value);
        // Clear immediately, including when the save fails.
        form.elements.tunnel_token.value = "";
        try { const saved = await h.requestJson("/api/settings/remote-access", {method: "PUT", ...json(body)}); busy = false; paint(saved); status.textContent = "Connection saved."; }
        catch (error) { busy = false; if (state) paint(state); status.textContent = error.message; }
        finally { body.tunnel_token = ""; }
    });
    if (global) global.addEventListener("change", async function () {
        busy = true; global.disabled = true;
        const status = document.getElementById("remote-global-status");
        try { await h.requestJson("/api/settings/remote-access/global", {method: "PUT", ...json({allowed: global.checked})}); busy = false; await refresh(); status.textContent = "Global setting saved."; }
        catch (error) { busy = false; if (state) paint(state); status.textContent = error.message; }
    });
    refresh().catch(failure);
    const timer = window.setInterval(() => { if (!busy && !document.hidden) refresh().catch(failure); }, 15000);
    window.addEventListener("pagehide", () => window.clearInterval(timer), {once: true});
});
