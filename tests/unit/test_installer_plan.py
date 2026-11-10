"""What a choice of pieces pulls in, and what it makes the page ask."""

from __future__ import annotations

from installer import plan

from tests.unit.installer_helpers import answers


def _keys(answer_set: dict) -> set[str]:
    return {q.key for q in plan.questions(answer_set)}


def test_ticking_the_terminal_clones_the_terminal_and_the_console_code_it_is_built_from():
    assert {"terminal", "hive"} <= set(plan.repos(answers(pieces=["terminal"])))
    # The console's code is cloned regardless — the first admin is seeded
    # with it — but the terminal's only when it is ticked.
    assert "terminal" not in plan.repos(answers(pieces=["console"]))


def test_ticking_a_bot_selects_the_console_where_it_is_claimed():
    assert "console" not in plan.closure([])
    assert "console" in plan.closure(["telegram"])
    assert "console" in plan.closure(["discord"])


def test_voice_is_installed_by_choosing_where_it_runs():
    assert "voice" not in plan.installed(answers())
    assert "voice" in plan.installed(answers(modes={"voice": "here"}))
    assert "voice" in plan.installed(answers(modes={"voice": "existing"}))
    assert "voice" not in plan.installed(answers(pieces=["voice"], modes={"voice": "none"}))


def test_ticking_a_bot_adds_its_token_and_only_its_token_to_the_questions():
    assert "telegram_token" not in _keys(answers())
    only_telegram = _keys(answers(pieces=["telegram"]))
    assert "telegram_token" in only_telegram
    assert "discord_token" not in only_telegram
    assert "discord_token" in _keys(answers(pieces=["discord"]))


def test_installing_the_proxy_here_asks_for_a_model_provider_and_its_key():
    here = _keys(answers())
    existing = _keys(answers(modes={"proxy": "existing"}))
    assert {"provider_kind", "provider_api_key", "provider_model"} <= here
    assert not {"provider_kind", "provider_api_key", "provider_model"} & existing
    assert {"proxy_url", "proxy_key"} <= existing


def test_a_mind_may_not_take_a_name_the_hive_uses_for_a_service():
    for name in ("voice", "hive-comms", "comms", "tools", "telegram"):
        assert any("name the hive uses" in p
                   for p in plan.validate(answers(mind_name=name))), name
    assert plan.validate(answers(mind_name="wren")) == []


def test_a_context_window_of_zero_is_refused():
    assert any("above zero" in p for p in plan.validate(answers(provider_model_window="0")))
