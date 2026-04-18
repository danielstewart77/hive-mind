"""Mind-local prompt composition for system prompts."""

from __future__ import annotations

from pathlib import Path


def _allowed_directories_block(allowed_directories: list[str] | None) -> str:
    if not allowed_directories:
        return ""

    lines = [f"- `{directory}`" for directory in allowed_directories]
    return (
        "You have been given access to the following project directories:\n"
        + "\n".join(lines)
    )


def _resolve_prompt_path(mind_dir: Path, relative_path: str) -> Path:
    path = (mind_dir / relative_path).resolve()
    root = mind_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Prompt file {relative_path!r} escapes mind directory {mind_dir}"
        ) from exc
    if not path.is_file():
        raise ValueError(f"Prompt file not found: {path}")
    return path


def _render_prompt_fragment(fragment: str, context: dict[str, str], source: Path) -> str:
    try:
        return fragment.format_map(context)
    except KeyError as exc:
        raise ValueError(
            f"Unknown prompt placeholder {exc.args[0]!r} in {source}"
        ) from exc


def build_prompt(
    *,
    date_str: str,
    mind_name: str,
    identity_block: str,
    soul_instruction: str,
    allowed_directories: list[str] | None,
    mind_dir: Path,
    prompt_files: list[str],
) -> str:
    """Compose the prompt from files declared in a mind's own folder."""
    if not prompt_files:
        raise ValueError(f"Mind {mind_name.lower()} does not declare any prompt_files")

    context = {
        "date_str": date_str,
        "mind_name": mind_name,
        "mind_id": mind_name.lower(),
        "identity_block": identity_block,
        "soul_instruction": soul_instruction,
        "security_spec_path": "/usr/src/app/specs/security.md",
        "email_signature": f"---\nSent on behalf of Daniel by {mind_name}.",
        "allowed_directories_block": _allowed_directories_block(allowed_directories),
    }

    sections: list[str] = []
    for relative_path in prompt_files:
        source = _resolve_prompt_path(mind_dir, relative_path)
        rendered = _render_prompt_fragment(
            source.read_text(encoding="utf-8"),
            context,
            source,
        ).strip()
        if rendered:
            sections.append(rendered)
    return "\n\n".join(sections)
