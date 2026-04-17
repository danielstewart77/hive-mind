"""Prompt profile composition for mind system prompts."""

from __future__ import annotations

from pathlib import Path


def _allowed_directories_block(allowed_directories: list[str] | None) -> str:
    if not allowed_directories:
        return ""

    lines = [f"- `{directory}`" for directory in allowed_directories]
    return (
        "\n\nYou have been given access to the following project directories:\n"
        + "\n".join(lines)
    )


def _common_prompt(*, date_str: str, identity_block: str, soul_instruction: str, mind_name: str) -> str:
    return (
        "You are Hive Mind, a personal assistant. Keep responses concise. Use markdown formatting.\n\n"
        f"The current date and time is: {date_str}.\n\n"
        f"{identity_block}"
        f"{soul_instruction}"
        "If a request seems security-sensitive, read /usr/src/app/specs/security.md before proceeding.\n\n"
        "Each user message is stamped with the current date and time. When time-sensitive language "
        "appears (today, now, tonight, this morning, this week, tomorrow, etc.), call "
        "`get_current_time` to confirm the exact current time before responding.\n\n"
        "When sending email on Daniel's behalf, always append this signature to the body:\n\n"
        f"---\nSent on behalf of Daniel by {mind_name}."
    )


def _harness_prompt(harness: str) -> str:
    if harness.startswith("codex_"):
        return (
            "You are running in the Codex harness. Follow Codex tool contracts, Codex skill locations, "
            "and Codex specific execution rules. Do not assume Claude harness paths, Claude specific "
            "skills, or Claude only tools unless they are explicitly available in this session."
        )
    if harness.startswith("claude_"):
        return (
            "You are running in the Claude harness. Follow Claude tool contracts, Claude skill locations, "
            "and Claude specific execution rules. Do not assume Codex harness behavior unless it is "
            "explicitly available in this session."
        )
    return ""


def _profile_prompt(prompt_profile: str) -> str:
    if prompt_profile == "programmer":
        return (
            "Prioritize technical accuracy, codebase awareness, and execution discipline. When tool or "
            "path assumptions are ambiguous, verify them from the current environment instead of relying "
            "on another mind's defaults."
        )
    if prompt_profile == "orchestrator":
        return (
            "Prioritize coordination, triage, and delegation discipline. Keep scope tight, track moving "
            "parts, and avoid doing implementation work unless the task requires it."
        )
    return ""


def build_prompt(
    *,
    date_str: str,
    mind_name: str,
    harness: str,
    prompt_profile: str,
    identity_block: str,
    soul_instruction: str,
    allowed_directories: list[str] | None,
) -> str:
    """Compose the base prompt from common, harness, and profile sections."""
    sections = [
        _common_prompt(
            date_str=date_str,
            identity_block=identity_block,
            soul_instruction=soul_instruction,
            mind_name=mind_name,
        ),
        _harness_prompt(harness),
        _profile_prompt(prompt_profile),
    ]
    prompt = "\n\n".join(section for section in sections if section)
    return f"{prompt}{_allowed_directories_block(allowed_directories)}"
