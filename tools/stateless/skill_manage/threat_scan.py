#!/usr/bin/env python3
"""Trimmed threat scanner for agent-authored skills (warn, never block).

A small port of Hermes' ``tools/threat_patterns.py``: classic prompt-injection,
exfil curl/wget, read-secrets, ssh-backdoor/authorized_keys, and hardcoded-secret
patterns, plus the invisible/bidirectional-unicode check. Skippy is an operator
mind, so ``skill_manage`` uses this to **annotate** flagged writes — it warns and
records ``flagged: true``, it never blocks the write.

``scan_for_threats(content) -> list[str]`` returns matched pattern IDs (and
``invisible_unicode_U+XXXX`` codepoints) so the caller can surface them.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# (regex, pattern_id) — trimmed to the classes named in the Phase 2 plan.
_PATTERNS: List[Tuple[str, str]] = [
    # Classic prompt injection
    (r"ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)", "disregard_rules"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    # Exfiltration via curl/wget with secrets
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    # Reading secrets files
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    # SSH backdoor / persistence
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access"),
    # Hardcoded secrets
    (r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}", "hardcoded_secret"),
]

_COMPILED: List[Tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), pid) for pat, pid in _PATTERNS
]

# Invisible / bidirectional unicode characters used in injection attacks.
INVISIBLE_CHARS = frozenset({
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "⁠",  # word joiner
    "⁢",  # invisible times
    "⁣",  # invisible separator
    "⁤",  # invisible plus
    "﻿",  # zero-width no-break space (BOM)
    "‪",  # left-to-right embedding
    "‫",  # right-to-left embedding
    "‬",  # pop directional formatting
    "‭",  # left-to-right override
    "‮",  # right-to-left override
    "⁦",  # left-to-right isolate
    "⁧",  # right-to-left isolate
    "⁨",  # first strong isolate
    "⁩",  # pop directional isolate
})


def scan_for_threats(content: str) -> List[str]:
    """Return the list of matched pattern IDs (+ invisible-unicode findings).

    Empty list means clean. Findings are warnings, not blocks — the caller
    (an operator mind) annotates and proceeds.
    """
    if not content:
        return []
    findings: List[str] = []
    for ch in (set(content) & INVISIBLE_CHARS):
        findings.append(f"invisible_unicode_U+{ord(ch):04X}")
    for compiled, pid in _COMPILED:
        if compiled.search(content):
            findings.append(pid)
    return findings


__all__ = ["INVISIBLE_CHARS", "scan_for_threats"]
