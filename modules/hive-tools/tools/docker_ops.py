"""Docker Ops FastAPI router."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from auth_api import require_api_token, require_api_token_or_person
from hitl import _check_hitl, hitl_gate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/docker", tags=["docker"])

# Basenames whose mutating operations are permanently blocked.
MUTATE_BLOCKED = {"hive_mind_mcp", "hive-tools"}


class BlockedProjectError(Exception):
    """Raised for a mutating operation on a project that may not be mutated.

    A class, because `server.py` has had a handler for this name since
    before the name existed: the import was wrapped in `except ImportError:
    pass`, so the handler was silently never registered and the one refusal
    it was written to shape has been answering `500 Internal Server Error`
    ever since. `docker compose up` on hive-tools' own directory is the
    service restarting itself mid-request, which is a refusal and not a
    fault.
    """


def _run_subprocess(
    cmd: list[str], cwd: str, env: dict[str, str] | None = None, timeout: int = 120
) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    run_env = {**os.environ, **(env or {})}
    try:
        result = subprocess.run(
            cmd, cwd=cwd, env=run_env, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"


def _validate_path(path: str) -> str | None:
    """Return error message if path is unusable, None if OK."""
    if not path:
        return "path is required"
    if not os.path.isdir(path):
        return f"Path not found: {path}"
    return None


def _compose_cmd(path: str, subcmd: list[str]) -> tuple[list[str], str]:
    """Build a docker compose command for the given path."""
    compose_file = os.path.join(path, "docker-compose.yml")
    cmd = ["docker", "compose", "-f", compose_file] + subcmd
    return cmd, path


def _result_json(rc: int, stdout: str, stderr: str) -> dict[str, Any]:
    return {
        "status": "success" if rc == 0 else "error",
        "return_code": rc,
        "stdout": stdout[-3000:] if len(stdout) > 3000 else stdout,
        "stderr": stderr[-3000:] if len(stderr) > 3000 else stderr,
    }


def docker_mutate_gate(tool_name: str):
    """Dependency for mutating docker endpoints.

    A mind's key, or a person's own key: the hive site restarts a mind for a
    signed-in person with that person's key, and the request waits on an
    approval as theirs."""

    # Register for default approval seeding just like other gated tools.
    # Keep the Docker dependency below: it records the query string that
    # selects the project, so the person sees which project they approve.
    hitl_gate(tool_name)

    async def _check(
        request: Request,
        path: str = Query(...),
        caller_mind: str = Depends(require_api_token_or_person),
    ) -> str:
        if os.path.basename(os.path.normpath(path)) in MUTATE_BLOCKED:
            raise BlockedProjectError(
                f"Mutating operations on '{path}' are blocked. This project "
                "runs the service handling the request, so composing it "
                "down or up from here would end the request that asked.")
        body_bytes = await request.body()
        qs = str(request.query_params)
        tool_path = f"{request.url.path}?{qs}" if qs else request.url.path
        return await _check_hitl(
            tool_name,
            caller_mind,
            tool_method=request.method,
            tool_path=tool_path,
            tool_body=body_bytes.decode("utf-8", errors="replace"),
            gone=request.is_disconnected,
        )

    return _check


# ---- Mutating endpoints (default HITL: on) ----


@router.post("/up")
async def compose_up(
    path: str = Query(...),
    service: str = "",
    caller: str = Depends(docker_mutate_gate("docker_compose_up")),
) -> Any:
    """Build and start a Docker Compose project."""
    err = _validate_path(path)
    if err:
        return {"error": err}
    subcmd = ["up", "-d", "--build"] + ([service] if service else [])
    cmd, cwd = _compose_cmd(path, subcmd)
    rc, stdout, stderr = _run_subprocess(cmd, cwd=cwd, timeout=300)
    return _result_json(rc, stdout, stderr)


@router.post("/restart")
async def compose_restart(
    path: str = Query(...),
    service: str = "",
    caller: str = Depends(docker_mutate_gate("docker_compose_restart")),
) -> Any:
    """Restart a Docker Compose service or whole project."""
    err = _validate_path(path)
    if err:
        return {"error": err}
    subcmd = ["restart"] + ([service] if service else [])
    cmd, cwd = _compose_cmd(path, subcmd)
    rc, stdout, stderr = _run_subprocess(cmd, cwd=cwd)
    return _result_json(rc, stdout, stderr)


@router.post("/down")
async def compose_down(
    path: str = Query(...),
    caller: str = Depends(docker_mutate_gate("docker_compose_down")),
) -> Any:
    """Stop and remove all containers in a project."""
    err = _validate_path(path)
    if err:
        return {"error": err}
    cmd, cwd = _compose_cmd(path, ["down"])
    rc, stdout, stderr = _run_subprocess(cmd, cwd=cwd)
    return _result_json(rc, stdout, stderr)


# ---- Read-only endpoints (default HITL: off) ----


@router.get("/logs")
async def compose_logs(
    path: str = Query(...),
    service: str = "",
    tail: int = 50,
    caller: str = Depends(hitl_gate("docker_compose_logs")),
) -> Any:
    """Fetch recent logs from a Docker Compose project."""
    err = _validate_path(path)
    if err:
        return {"error": err}
    tail = max(1, min(tail, 500))
    subcmd = ["logs", "--tail", str(tail), "--no-color"] + ([service] if service else [])
    cmd, cwd = _compose_cmd(path, subcmd)
    rc, stdout, stderr = _run_subprocess(cmd, cwd=cwd, timeout=15)
    output = stdout + stderr
    return {
        "status": "success" if rc == 0 else "error",
        "path": path,
        "service": service or "(all)",
        "lines": tail,
        "logs": output[-5000:] if len(output) > 5000 else output,
    }


@router.get("/status")
async def compose_status(
    path: str = Query(...),
    caller: str = Depends(hitl_gate("docker_compose_status")),
) -> Any:
    """Show running containers in a project."""
    err = _validate_path(path)
    if err:
        return {"error": err}
    cmd, cwd = _compose_cmd(path, ["ps", "--format", "json"])
    rc, stdout, stderr = _run_subprocess(cmd, cwd=cwd, timeout=10)

    if rc != 0:
        return {"error": stderr or "Failed to get status"}

    services: list[dict[str, str]] = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            svc = json.loads(line)
            services.append({
                "service": svc.get("Service", ""),
                "name": svc.get("Name", ""),
                "state": svc.get("State", ""),
                "status": svc.get("Status", ""),
                "health": svc.get("Health", ""),
            })
        except json.JSONDecodeError:
            continue

    return {"path": path, "services": services}


@router.get("/containers")
async def list_containers(
    show_all: bool = False,
    caller: str = Depends(hitl_gate("docker_list_containers")),
) -> Any:
    """List Docker containers on the host."""
    cmd = ["docker", "ps", "--format", "json"] + (["--all"] if show_all else [])
    rc, stdout, stderr = _run_subprocess(cmd, cwd="/tmp", timeout=10)
    if rc != 0:
        return {"error": stderr or "Failed to list containers"}

    containers: list[dict[str, str]] = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
            containers.append({
                "name": c.get("Names", ""),
                "image": c.get("Image", ""),
                "state": c.get("State", ""),
                "status": c.get("Status", ""),
                "ports": c.get("Ports", ""),
            })
        except json.JSONDecodeError:
            continue

    return {"containers": containers, "count": len(containers)}


@router.get("/networks")
async def list_networks(
    caller: str = Depends(hitl_gate("docker_list_networks")),
) -> Any:
    """List all Docker networks."""
    rc, stdout, stderr = _run_subprocess(
        ["docker", "network", "ls", "--format", "json"], cwd="/tmp", timeout=10
    )
    if rc != 0:
        return {"error": stderr or "Failed to list networks"}

    networks: list[dict[str, str]] = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            n = json.loads(line)
            networks.append({
                "name": n.get("Name", ""),
                "driver": n.get("Driver", ""),
                "scope": n.get("Scope", ""),
            })
        except json.JSONDecodeError:
            continue

    return {"networks": networks, "count": len(networks)}
