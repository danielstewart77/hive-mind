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
    randomize_blocks,
    randomize_text,
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
        # Fetching a secret is the form we want the model to imitate. No
        # credential contains a bracket or a parenthesis, so a value that
        # does is code, not a value.
        "token = os.environ['GITHUB_TOKEN']",
        'api_key = os.getenv("ANTHROPIC_API_KEY")',
        "const key = process.env.OPENAI_API_KEY.trim()",
        "password = keyring.get_password('planka', 'admin')",
        # What a .env.example is made of.
        "DISCORD_BOT_TOKEN=your-discord-bot-token",
        "SMTP_PASSWORD=your-app-password",
        "API_KEY=replace_me_here",
        # An endpoint, not a credential.
        '_LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"',
        # Passing a secret is not stating one.
        "bearer_token=args.bearer_token",
        # The assignment rule stops at the space, capturing a header name.
        'AUTH="Authorization: Bearer $LUCENT_BEARER_TOKEN"',
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


# --- randomization -------------------------------------------------------
#
# The middle option: neither the real credential nor a placeholder slug, but
# a different string of the same shape, so the model learns what a token
# looks like without learning any actual one.


def test_surrogate_keeps_length_and_character_class():
    original = "ghp_AbCd1234efGH5678ijKL9012mnOP3456"
    out = randomize_text(f"GITHUB_TOKEN={original}")
    surrogate = out.split("=", 1)[1]

    assert surrogate != original
    assert len(surrogate) == len(original)
    assert surrogate.startswith("ghp_")
    for a, b in zip(original, surrogate):
        assert a.isdigit() == b.isdigit()
        assert a.isalpha() == b.isalpha()
        assert a.isupper() == b.isupper()


def test_the_real_credential_is_gone():
    original = "sk-ant-api03-QQQQwwwwEEEErrrrTTTTyyyy1234"
    assert original not in randomize_text(f"key is {original}")


def test_no_redaction_slug_is_emitted():
    """The whole point: the model must not learn a placeholder token."""
    out = randomize_text("GITHUB_TOKEN=ghp_AbCd1234efGH5678ijKL9012mnOP3456")
    assert "REDACTED" not in out


def test_the_same_secret_maps_to_the_same_surrogate_everywhere():
    """A token in two hundred turns must not become two hundred tokens."""
    text = "GITHUB_TOKEN=ghp_AbCd1234efGH5678ijKL9012mnOP3456"
    assert randomize_text(text) == randomize_text(text)

    pair = randomize_text(f"{text}\nand again {text}")
    first, second = pair.split("\nand again ")
    assert first.split("=", 1)[1] == second.split("=", 1)[1]


def test_different_salts_give_different_surrogates():
    text = "GITHUB_TOKEN=ghp_AbCd1234efGH5678ijKL9012mnOP3456"
    assert randomize_text(text, salt="a") != randomize_text(text, salt="b")


def test_bearer_scheme_survives_randomization():
    out = randomize_text("Authorization: Bearer abcdefghijklmnopqrstuvwx")
    assert out.startswith("Authorization: Bearer ")
    assert "abcdefghijklmnopqrstuvwx" not in out
    assert len(out) == len("Authorization: Bearer abcdefghijklmnopqrstuvwx")


def test_private_keys_are_dropped_rather_than_surrogated():
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    assert randomize_text(text) == "<REDACTED_PRIVATE_KEY>"


def test_randomize_leaves_ordinary_code_alone():
    for text in ("def login(user: str, password: str) -> bool:", "api_key = None"):
        assert randomize_text(text) == text


def test_randomize_blocks_covers_nested_tool_input_without_mutating():
    blocks = [
        {"type": "text", "text": "token is ghp_AbCd1234efGH5678ijKL9012mnOP3456"},
        {
            "type": "tool_use",
            "name": "Bash",
            "id": "t1",
            "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234'"},
        },
    ]
    original = json.dumps(blocks)
    out = randomize_blocks(blocks)

    assert json.dumps(blocks) == original, "input must not be mutated"
    serialized = json.dumps(out)
    assert "ghp_AbCd1234efGH5678ijKL9012mnOP3456" not in serialized
    assert "abcdefghijklmnop1234" not in serialized
    assert "REDACTED" not in serialized
    assert out[1]["name"] == "Bash"
