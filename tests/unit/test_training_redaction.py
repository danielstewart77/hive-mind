"""Unit tests for credential redaction over the training corpus.

The corpus is captured losslessly from real tool output, so it contains real
credentials. These tests pin the two properties that matter: a credential is
detected and replaced, and ordinary code is left intact.
"""

from __future__ import annotations

import json

import pytest

from core.training_redaction import (
    count_secrets_in_row,
    find_secrets,
    redact_blocks,
    redact_text,
)


@pytest.mark.parametrize(
    "text",
    [
        "export ANTHROPIC_API_KEY=sk-ant-api03-AAAAbbbbCCCCddddEEEEffff1234",
        "remote add origin https://ghp_EXAMPLEfake0000TOKENfor0TESTS0000000@github.com/x/y",
        "curl -H 'Authorization: Bearer 0000EXAMPLEbearer0000fake0000token00'",
        "PLANKA_ADMIN_PASSWORD=example-fake-pw",
        "COMMS_BEARER_TOKEN=EXAMPLEfakeCommsBearerTokenValue0",
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "AWS key AKIAIOSFODNN7EXAMPLE in the profile",
        "clone https://daniel:examplefakepw@git.internal/repo.git",
    ],
)
def test_detects_and_replaces_credentials(text):
    assert find_secrets(text), f"no detection for: {text[:40]}"
    cleaned = redact_text(text)
    assert "REDACTED" in cleaned
    assert cleaned != text


@pytest.mark.parametrize(
    "text",
    [
        "def login(user: str, password: str) -> bool:",
        "api_key = None",
        "The password must be at least eight characters.",
        "AUTH_TOKEN=$COMMS_BEARER_TOKEN",
        "SECRET_KEY=${SECRET_KEY}",
        "GITHUB_TOKEN=<REDACTED_SECRET>",
        "raise ValueError('password prompt suppressed')",
    ],
)
def test_leaves_ordinary_code_and_prose_alone(text):
    assert redact_text(text) == text


def test_private_key_block_is_removed_whole():
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    cleaned = redact_text(text)
    assert cleaned == "<REDACTED_PRIVATE_KEY>"
    assert "b3BlbnNz" not in cleaned


def test_bearer_keyword_is_preserved_so_shape_is_still_learnable():
    cleaned = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwx")
    assert cleaned == "Authorization: Bearer <REDACTED_BEARER>"


def test_preview_never_reveals_the_secret():
    hits = find_secrets("GITHUB_TOKEN=ghp_EXAMPLEfake0000TOKENfor0TESTS0000000")
    assert hits
    for hit in hits:
        assert "EXAMPLEfake0000TOKENfor0TESTS" not in hit.preview
        assert "chars)" in hit.preview


def test_redact_blocks_covers_text_content_and_nested_tool_input():
    blocks = [
        {"type": "thinking", "text": "the key is sk-ant-api03-ZZZZyyyyXXXXwwww1234"},
        {
            "type": "tool_use",
            "name": "Bash",
            "id": "t1",
            "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234'"},
        },
        {
            "type": "tool_result",
            "tool_call_id": "t1",
            "content": "GITHUB_TOKEN=ghp_AAAAbbbbCCCCddddEEEE",
        },
    ]
    original = json.dumps(blocks)
    cleaned = redact_blocks(blocks)

    assert json.dumps(blocks) == original, "input must not be mutated"
    serialized = json.dumps(cleaned)
    assert "sk-ant-api03-ZZZZ" not in serialized
    assert "ghp_AAAAbbbb" not in serialized
    assert "abcdefghijklmnop1234" not in serialized
    assert cleaned[1]["name"] == "Bash"


def test_redact_blocks_keeps_structure_intact():
    blocks = [
        {"type": "text", "text": "running it"},
        {"type": "tool_use", "name": "Read", "id": "r1", "input": {"path": "/tmp/x"}},
        {"type": "tool_result", "tool_call_id": "r1", "content": "hello"},
    ]
    cleaned = redact_blocks(blocks)
    assert [b["type"] for b in cleaned] == ["text", "tool_use", "tool_result"]
    assert cleaned[1]["input"] == {"path": "/tmp/x"}


def test_count_secrets_in_row_spans_both_columns():
    hits = count_secrets_in_row(
        "here is my key sk-ant-api03-QQQQwwwwEEEErrrr1234",
        json.dumps([{"type": "text", "text": "ghp_AAAAbbbbCCCCddddEEEE"}]),
    )
    assert {h.rule for h in hits} >= {"anthropic_key", "github_token"}


def test_empty_input_is_safe():
    assert redact_text("") == ""
    assert find_secrets("") == []
    assert redact_blocks([]) == []
