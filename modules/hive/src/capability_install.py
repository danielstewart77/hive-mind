"""Narrow, fixed-source installation helpers for capability repositories."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

INFERENCE_PROXY_REPOSITORY = "https://github.com/danielstewart77/inference-proxy.git"
DEFAULT_INFERENCE_PROXY_DIR = Path("/inference_proxy_repo")


def inference_proxy_directory() -> Path:
    return Path(os.getenv("INFERENCE_PROXY_REPO_DIR", str(DEFAULT_INFERENCE_PROXY_DIR)))


def inference_proxy_status() -> dict:
    path = inference_proxy_directory()
    installed = path.is_dir() and (path / ".git").is_dir()
    return {
        "installed": installed,
        "path": str(path),
        "repository": INFERENCE_PROXY_REPOSITORY,
    }


def clone_inference_proxy() -> dict:
    """Clone the fixed public repository into the mounted capability path."""
    path = inference_proxy_directory()
    if path.is_symlink():
        raise ValueError("The inference proxy destination may not be a symbolic link")
    if (path / ".git").is_dir():
        return inference_proxy_status()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("The inference proxy destination is not empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", INFERENCE_PROXY_REPOSITORY, str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed in the Hive container") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The inference proxy clone timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "Git clone failed").strip()
        raise RuntimeError(detail[-500:]) from exc
    return inference_proxy_status()
