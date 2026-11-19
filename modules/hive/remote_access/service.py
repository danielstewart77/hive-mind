"""Private host control socket and account-scoped HTTP/WebSocket ingress."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import grp
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from .manager import Manager, read_json

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def make_control(manager: Manager) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ValueError)
    async def bad_request(request, exception):
        return JSONResponse({"detail": str(exception)}, status_code=400)

    @app.get("/users/{owner}")
    def status(owner: str):
        return manager.status(owner)

    @app.put("/users/{owner}")
    def configure(owner: str, body: dict):
        return manager.configure(owner, body)

    @app.post("/users/{owner}/{action}")
    def set_state(owner: str, action: str):
        if action not in {"on", "off"}:
            raise HTTPException(400, "Choose on or off")
        return manager.arm(owner) if action == "on" else manager.off(owner)

    @app.get("/global")
    def global_status():
        return {"allowed": manager.allowed(), "timers_ready": manager.timers_ready()}

    @app.put("/global")
    def global_setting(body: dict):
        return manager.set_allowed(body.get("allowed"))

    return app


def make_gateway(manager: Manager, *, transport=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def connection(request):
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        for config in manager.configurations():
            for field, origin in (("website_url", manager.site_origin), ("terminal_url", manager.terminal_origin)):
                if urlsplit(config[field]).hostname == host and manager.active(config["owner"]):
                    record = manager.running(config["owner"])
                    if record and record.get("phase") == "armed":
                        return config, origin, field
        return None

    def headers(request, config, *, website=True):
        # Neither client-supplied remote identity nor bearer credentials can
        # choose the account whose tunnel this hostname belongs to.
        values = {key: value for key, value in request.headers.items()
                  if key.lower() not in HOP_HEADERS and not key.lower().startswith("x-hive-remote-")}
        values.update({"x-forwarded-proto": "https", "x-hive-remote-owner": config["owner"]})
        if website:
            values.update({"x-hive-remote-key": read_json(manager.root / "verifier.key")["key"],
                           "x-hive-remote-terminal": config["terminal_url"]})
        return values

    async def authorize(request, config):
        async with httpx.AsyncClient(transport=transport, timeout=5) as client:
            response = await client.post(manager.site_origin + "/api/remote-access/authorize",
                headers={"cookie": request.headers.get("cookie", ""), "x-hive-remote-key": read_json(manager.root / "verifier.key")["key"]},
                json={"owner": config["owner"]})
            return response.status_code

    def public(path):
        return path.startswith("/static/") or path in {"/login", "/logout", "/password", "/health", "/login/microsoft", "/login/microsoft/callback", "/login/windows"}

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(request: Request, path: str):
        selected = connection(request)
        if not selected:
            return JSONResponse({"detail": "Remote access is closed for this address"}, status_code=403)
        config, origin, field = selected
        if not public(request.url.path):
            try:
                status = await authorize(request, config)
            except httpx.HTTPError:
                return JSONResponse({"detail": "Hive sign-in is unavailable"}, status_code=503)
            if status != 200:
                if status == 401 and request.method in {"GET", "HEAD"} and not request.url.path.startswith("/api/"):
                    return RedirectResponse("/login", status_code=303)
                return JSONResponse({"detail": "Sign in as the owner of this remote connection"}, status_code=403 if status == 403 else 401)
        target = origin + request.scope["raw_path"].decode("ascii")
        if request.scope["query_string"]:
            target += "?" + request.scope["query_string"].decode("ascii")
        client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10, read=None), follow_redirects=False)
        try:
            upstream = await client.send(client.build_request(request.method, target, headers=headers(request, config, website=field == "website_url"), content=request.stream()), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse({"detail": "This Hive service is not answering"}, status_code=502)

        async def content():
            try:
                async for chunk in upstream.aiter_raw():
                    if not manager.active(config["owner"]):
                        break
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        html = upstream.headers.get("content-type", "").lower().startswith("text/html")
        if html:
            try:
                body = await upstream.aread()
                # Origin servers may render absolute HTTP asset URLs because
                # the final local hop is HTTP. Public pages must stay HTTPS.
                host = urlsplit(config[field]).hostname
                body = body.replace(("http://" + host + "/").encode(), (config[field] + "/").encode())
                response = Response(body, status_code=upstream.status_code)
            finally:
                await upstream.aclose()
                await client.aclose()
        else:
            response = StreamingResponse(content(), status_code=upstream.status_code)
        values = []
        for key, value in upstream.headers.multi_items():
            if key.lower() in HOP_HEADERS:
                continue
            if html and key.lower() in {"content-length", "content-encoding"}:
                continue
            if key.lower() == "set-cookie":
                # A personal hostname cannot set the installation's LAN cookie
                # domain. Each public origin gets its own host-only session.
                value = re.sub(r";\s*Domain=[^;]*", "", value, flags=re.I)
            if key.lower() == "location":
                parsed = urlsplit(value)
                if parsed.hostname in {urlsplit(origin).hostname, urlsplit(config[field]).hostname}:
                    value = config[field] + parsed.path + ("?" + parsed.query if parsed.query else "")
            values.append((key.encode("latin-1"), value.encode("latin-1")))
        response.raw_headers = values
        return response

    @app.websocket("/{path:path}")
    async def proxy_websocket(socket: WebSocket, path: str):
        selected = connection(socket)
        if not selected:
            await socket.close(code=4403)
            return
        config, origin, field = selected
        try:
            if await authorize(socket, config) != 200:
                await socket.close(code=4403)
                return
            target = origin.replace("http://", "ws://", 1) + socket.scope["raw_path"].decode("ascii")
            if socket.scope["query_string"]:
                target += "?" + socket.scope["query_string"].decode("ascii")
            forwarded = headers(socket, config, website=field == "website_url")
            for key in list(forwarded):
                if key.lower() in {"host", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}:
                    del forwarded[key]
            async with websockets.connect(target, additional_headers=forwarded, proxy=None, max_size=None) as upstream:
                await socket.accept()
                async def inbound():
                    while True:
                        event = await socket.receive()
                        if event["type"] == "websocket.disconnect":
                            return
                        await upstream.send(event.get("bytes") if event.get("bytes") is not None else event.get("text", ""))
                async def outbound():
                    async for event in upstream:
                        await socket.send_bytes(event) if isinstance(event, bytes) else await socket.send_text(event)
                async def expiration():
                    while manager.active(config["owner"]):
                        await asyncio.sleep(0.5)
                tasks = [asyncio.create_task(function()) for function in (inbound, outbound, expiration)]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (httpx.HTTPError, OSError, websockets.WebSocketException):
            pass
        finally:
            try:
                await socket.close(code=1000 if manager.active(config["owner"]) else 4403)
            except RuntimeError:
                pass  # A browser disconnect already completed the socket.

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--cloudflared", default="cloudflared")
    parser.add_argument("--site-origin", default="http://127.0.0.1:8710")
    parser.add_argument("--terminal-origin", default="http://127.0.0.1:8711")
    parser.add_argument("--gateway-port", type=int, default=49310)
    parser.add_argument("--unit-prefix", default="hive-remote-access")
    parser.add_argument("--connector-user")
    parser.add_argument("--website-group")
    parser.add_argument("--system", action="store_true")
    parser.add_argument("--off-all", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    args = parser.parse_args()
    manager = Manager(args.state_dir, args.cloudflared, site_origin=args.site_origin, terminal_origin=args.terminal_origin,
                      gateway_origin=f"http://127.0.0.1:{args.gateway_port}", timer_prefix=args.unit_prefix,
                      connector_user=args.connector_user, system=args.system)
    if args.off_all:
        manager.off_all()
        return
    if args.sweep:
        manager.sweep()
        return
    if not args.system or not args.connector_user or os.geteuid() != 0:
        parser.error("The host controller requires root, --system, and a dedicated --connector-user")
    # Restarting the host controller never silently republishes a user's tunnel.
    manager.off_all()
    from .egress import install
    install(args.connector_user, args.gateway_port)
    os.umask(0o077)
    socket = manager.root / "control.sock"
    socket.unlink(missing_ok=True)
    if args.website_group:
        group = grp.getgrnam(args.website_group).gr_gid
        os.chown(manager.root, 0, group)
        os.chmod(manager.root, 0o750)
        os.chown(manager.root / "verifier.key", 0, group)
        os.chmod(manager.root / "verifier.key", 0o640)
        def share_socket():
            import time
            while not socket.exists():
                time.sleep(0.02)
            os.chown(socket, 0, group)
            os.chmod(socket, 0o660)
        threading.Thread(target=share_socket, daemon=True).start()
    threading.Thread(target=uvicorn.run, args=(make_gateway(manager),), kwargs={"host": "127.0.0.1", "port": args.gateway_port, "access_log": False}, daemon=True).start()
    try:
        uvicorn.run(make_control(manager), uds=str(socket), access_log=False)
    finally:
        manager.off_all()


if __name__ == "__main__":
    main()
