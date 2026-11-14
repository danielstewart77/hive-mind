"""The mind-local rotation percentage and its context-window calculation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

ROTATION_THRESHOLD_FIELD = "rotation_threshold_percent"
DEFAULT_PERCENTAGE = 100


class InvalidRotationPolicy(ValueError):
    """A present mind preference is not a whole-number percentage from 1 to 100."""


def effective_percent(runtime: Mapping[str, Any] | Any) -> int:
    """Return the saved percentage, using 100 only when the key is absent."""
    if not isinstance(runtime, Mapping):
        raise InvalidRotationPolicy("runtime configuration must be a mapping")
    if ROTATION_THRESHOLD_FIELD not in runtime:
        return DEFAULT_PERCENTAGE
    value = runtime[ROTATION_THRESHOLD_FIELD]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise InvalidRotationPolicy("rotation threshold must be a whole number from 1 to 100")
    return value


def effective_threshold(context_window: Any, percentage: Any) -> int | None:
    """Return the established floor-sized threshold, retaining a known zero."""
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        return None
    if isinstance(percentage, bool) or not isinstance(percentage, int) or not 1 <= percentage <= 100:
        return None
    return context_window * percentage // 100


def read_policy(path_or_runtime: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Read and validate policy; an invalid file never becomes the default."""
    try:
        if isinstance(path_or_runtime, Mapping):
            runtime = path_or_runtime
        else:
            runtime = yaml.safe_load(Path(path_or_runtime).read_text())
        if not isinstance(runtime, Mapping):
            raise InvalidRotationPolicy("runtime configuration must be a mapping")
        percentage = effective_percent(runtime)
    except OSError:
        return {
            "status": "invalid", "percentage": None,
            "error": "invalid_runtime_policy", "reason": "config_unreadable",
        }
    except yaml.YAMLError:
        return {"status": "invalid", "percentage": None, "error": "invalid_runtime_policy"}
    except InvalidRotationPolicy:
        return {"status": "invalid", "percentage": None, "error": "invalid_runtime_policy"}
    return {"status": "ok", "percentage": percentage, "error": None}
