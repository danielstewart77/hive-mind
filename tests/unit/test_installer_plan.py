"""What a choice of pieces pulls in, and what it makes the page ask."""

from __future__ import annotations

from installer import plan

from tests.unit.installer_helpers import answers


def _keys(answer_set: dict) -> set[str]:
    return {q.key for q in plan.questions(answer_set)}


def test_ticking_the_terminal_clones_the_console_code_it_is_built_from():
    assert "hive" in plan.repos(answers(pieces=["terminal"]))


def test_ticking_a_bot_selects_the_console_where_it_is_claimed():
    assert "console" in plan.closure(["telegram"])
    assert "console" in plan.closure(["discord"])


def test_ticking_a_bot_adds_its_token_to_the_questions():
    assert "telegram_token" not in _keys(answers())
    assert "telegram_token" in _keys(answers(pieces=["telegram"]))
    assert "discord_token" in _keys(answers(pieces=["discord"]))


def test_installing_the_proxy_here_asks_for_a_model_provider_and_its_key():
    here = _keys(answers())
    existing = _keys(answers(modes={"proxy": "existing"}))
    assert {"provider_kind", "provider_api_key", "provider_model"} <= here
    assert not {"provider_kind", "provider_api_key", "provider_model"} & existing
    assert {"proxy_url", "proxy_key"} <= existing
