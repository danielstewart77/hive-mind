"""Credential redaction for the training corpus.

The raw store is lossless by contract: ``training_capture`` writes exactly
what the harness emitted, which means real tool output — including
``cat .env``, ``git remote -v`` with a token in the URL, and every bearer
header a mind ever curled — lands in ``assistant_blocks`` verbatim. That is
the correct choice for a capture layer and a catastrophic one for a training
set, because a fine-tuned model memorizes and re-emits high-entropy strings
it saw during training.

This module is the boundary. It never mutates the raw store. It offers two
services over an in-memory copy of a row:

- :func:`find_secrets` — detection, used by curation to flag rows so a human
  can see how much of the corpus is contaminated.
- :func:`redact_text` / :func:`redact_blocks` — replacement, applied at
  export time so the JSONL that reaches a trainer carries placeholders
  instead of live credentials.

Detection is deliberately over-eager. A false positive costs one redacted
token in a training example; a false negative costs a leaked credential
baked into model weights, which cannot be revoked by editing a file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Each rule is (name, compiled pattern, placeholder). Patterns capture the
# whole secret so the replacement swaps the entire match. Where a secret is
# introduced by a keyword (``PASSWORD=``), the keyword is captured in group
# 1 and preserved, so the redacted text still reads as an assignment and the
# model learns the *shape* of the operation without the value.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "anthropic_key",
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
        "<REDACTED_ANTHROPIC_KEY>",
    ),
    (
        "openai_key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
        "<REDACTED_OPENAI_KEY>",
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
        "<REDACTED_GITHUB_TOKEN>",
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "<REDACTED_AWS_KEY>",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
        "<REDACTED_SLACK_TOKEN>",
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        "<REDACTED_PRIVATE_KEY>",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        "<REDACTED_JWT>",
    ),
    (
        "bearer_header",
        re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{16,}"),
        r"\1<REDACTED_BEARER>",
    ),
    (
        "url_credentials",
        re.compile(r"(?i)\b((?:https?|ftp)://)[^\s/:@]+:[^\s/@]+(@)"),
        r"\1<REDACTED_USER>:<REDACTED_PASSWORD>\2",
    ),
    # Keyword-introduced assignments: KEY=value, "key": "value", key: value.
    # The keyword and its separator are preserved; only the value is swapped.
    (
        "env_assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|"
            r"PRIVATE_?KEY|CLIENT_?SECRET|AUTH)[A-Z0-9_]*\s*[=:]\s*)"
            r"(?![\s\"']*(?:$|\n))"
            r"[\"']?([^\s\"',}{)\]]{6,})[\"']?"
        ),
        r"\1<REDACTED_SECRET>",
    ),
]

# ``env_assignment`` matches Python source as readily as it matches a .env
# file (``password: str`` in a signature, ``api_key = None``). These values
# are structural, not secret, and redacting them would corrupt code examples
# that are the whole point of the dataset.
_ASSIGNMENT_ALLOWED_VALUES = frozenset(
    {
        "none",
        "null",
        "nil",
        "true",
        "false",
        "str",
        "int",
        "bool",
        "float",
        "bytes",
        "string",
        "optional",
        "empty",
        "unset",
        "changeme",
        "redacted",
        "your_token_here",
        "xxx",
        "...",
        # Auth *schemes*, not credentials. ``Authorization: Bearer <token>``
        # matches the keyword rule because the header name ends in AUTH, and
        # without these the scheme word gets scrubbed and the header stops
        # reading as a header. The credential after it is handled by the
        # dedicated ``bearer_header`` rule, which runs first.
        "bearer",
        "basic",
        "digest",
        "negotiate",
        "token",
    }
)

_PLACEHOLDER_RE = re.compile(r"^<REDACTED_[A-Z_]+>$|^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")


@dataclass(frozen=True)
class SecretHit:
    """One detected credential. ``preview`` is safe to log and display."""

    rule: str
    preview: str

    @staticmethod
    def _preview(value: str) -> str:
        """First four characters plus a length, never the secret itself."""
        stripped = value.strip()
        head = stripped[:4]
        return f"{head}…({len(stripped)} chars)"


def _assignment_value_is_secret(value: str) -> bool:
    """False for type annotations, placeholders, and env-var indirection."""
    cleaned = value.strip().strip("\"'")
    if not cleaned or len(cleaned) < 6:
        return False
    if cleaned.lower() in _ASSIGNMENT_ALLOWED_VALUES:
        return False
    # ``TOKEN=$COMMS_BEARER_TOKEN`` and ``TOKEN=<REDACTED_SECRET>`` are the
    # safe forms we want the model to imitate, not values to scrub.
    if _PLACEHOLDER_RE.match(cleaned):
        return False
    return True


def find_secrets(text: str) -> list[SecretHit]:
    """Return every credential detected in ``text``, without revealing it."""
    if not text:
        return []
    hits: list[SecretHit] = []
    for name, pattern, _ in _RULES:
        for match in pattern.finditer(text):
            if name == "env_assignment":
                if not _assignment_value_is_secret(match.group(2)):
                    continue
                secret = match.group(2)
            elif name in {"bearer_header", "url_credentials"}:
                secret = match.group(0)
            else:
                secret = match.group(0)
            hits.append(SecretHit(rule=name, preview=SecretHit._preview(secret)))
    return hits


def redact_text(text: str) -> str:
    """Replace every detected credential in ``text`` with a placeholder."""
    if not text:
        return text
    result = text
    for name, pattern, replacement in _RULES:
        if name == "env_assignment":

            def _sub(match: re.Match[str]) -> str:
                if not _assignment_value_is_secret(match.group(2)):
                    return match.group(0)
                return f"{match.group(1)}<REDACTED_SECRET>"

            result = pattern.sub(_sub, result)
        else:
            result = pattern.sub(replacement, result)
    return result


def redact_blocks(blocks: list[dict]) -> list[dict]:
    """Redact every text-bearing field of an ``assistant_blocks`` array.

    Returns a new list; the input is not mutated. ``tool_use.input`` is
    redacted through its JSON serialization so a credential passed as a
    command argument (``curl -H "Authorization: Bearer …"``) is caught
    regardless of how deeply it is nested.
    """
    redacted: list[dict] = []
    for block in blocks:
        item = dict(block)
        if isinstance(item.get("text"), str):
            item["text"] = redact_text(item["text"])
        if isinstance(item.get("content"), str):
            item["content"] = redact_text(item["content"])
        raw_input = item.get("input")
        if raw_input is not None:
            serialized = json.dumps(raw_input, ensure_ascii=False)
            cleaned = redact_text(serialized)
            if cleaned != serialized:
                try:
                    item["input"] = json.loads(cleaned)
                except json.JSONDecodeError:
                    # A placeholder broke the JSON (a secret spanning a
                    # quote boundary). Keep the row usable by dropping the
                    # arguments rather than shipping the credential.
                    item["input"] = {"_redacted": True}
        redacted.append(item)
    return redacted


def count_secrets_in_row(user_content: str, assistant_blocks: str) -> list[SecretHit]:
    """Detect credentials across both text-bearing columns of a turn row."""
    return find_secrets(user_content or "") + find_secrets(assistant_blocks or "")
