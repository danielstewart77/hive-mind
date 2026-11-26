"""What a mind's own bot must present to collect its proactive messages."""

from core import mind_key

OLD_GATEWAY = "old-gateway-value-0123456789"  # secret-guard: allow - invented fixture
ONE_KEY = "the-one-key-0123456789"  # secret-guard: allow - invented fixture
OWN = "the-bots-own-0123456789"  # secret-guard: allow - invented fixture


def test_a_mind_on_its_one_key_never_accepts_the_old_gateway_value_from_its_bot():
    token = mind_key.proactive_token({"MIND_KEY": ONE_KEY}, OLD_GATEWAY)

    assert token and token not in (OLD_GATEWAY, ONE_KEY)


def test_a_mind_on_its_one_key_accepts_the_bots_own_credential():
    env = {"MIND_KEY": ONE_KEY, "MIND_PROACTIVE_TOKEN": OWN}

    assert mind_key.proactive_token(env, OLD_GATEWAY) == OWN


def test_a_mind_not_yet_moved_keeps_comparing_against_the_value_it_started_with():
    assert mind_key.proactive_token({}, OLD_GATEWAY) == OLD_GATEWAY
