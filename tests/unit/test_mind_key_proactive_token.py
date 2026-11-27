"""What a mind's own bot must present to collect its proactive messages: the
mind's own key, and no credential of the bot's own."""

from core import mind_key

OLD_GATEWAY = "old-gateway-value-0123456789"  # secret-guard: allow - invented fixture
ONE_KEY = "the-one-key-0123456789"  # secret-guard: allow - invented fixture
OTHER = "a-bots-leftover-0123456789"  # secret-guard: allow - invented fixture


def test_a_mind_on_its_one_key_is_polled_with_that_key_and_not_the_old_gateway_value():
    assert mind_key.proactive_token({"MIND_KEY": ONE_KEY}, OLD_GATEWAY) == ONE_KEY


def test_a_leftover_bot_credential_is_not_what_the_mind_asks_for():
    env = {"MIND_KEY": ONE_KEY, "MIND_PROACTIVE_TOKEN": OTHER}

    assert mind_key.proactive_token(env, OLD_GATEWAY) == ONE_KEY


def test_a_mind_not_yet_moved_keeps_comparing_against_the_value_it_started_with():
    assert mind_key.proactive_token({}, OLD_GATEWAY) == OLD_GATEWAY
