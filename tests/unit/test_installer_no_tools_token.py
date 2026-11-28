"""Wave 8 finding 1, 2 and 5: the installer mints no tools token, gives no site
or tool a gateway key that stands for anyone, and tells the tools where the
hive site is. Rendered by the installer's own `compose.render` and run through
its own `mint_credentials` step (no docker needed)."""

from __future__ import annotations

from installer import compose, steps

from tests.unit.installer_helpers import answers, installer_at


class _Shell:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        raise AssertionError(f"the credentials step ran a command it must not: {argv}")


def test_the_credentials_step_mints_for_the_services_that_hold_one_and_never_asks_the_tools_for_a_token(tmp_path, monkeypatch):
    inst = installer_at(tmp_path, answers(pieces=["console", "terminal", "tools"]))
    ctx = steps.Ctx(inst, _Shell())
    minted = []
    monkeypatch.setattr(steps.hive_api, "mint", lambda sh, dsn, image, store, wanted, admin, **kw:
                        minted.append((store, sorted(wanted))) or {h: "minted" for h in wanted})
    monkeypatch.setattr(ctx, "hive_db", lambda: ("postgresql://x", "image"))
    monkeypatch.setattr(ctx, "env", lambda: {
        compose.token_var(store.upper(), h): "value-0123456789"
        for store in ("comms", "lucent") for h in (compose.COMMS, compose.CONSOLE)})

    done = steps.mint_credentials(ctx)

    assert set(done) == {"comms", "lucent"}
    assert "tools" not in done
    # Nothing was run in a container: no `exec ... generate_token`.
    assert ctx.sh.calls == []
    # The gateway: no service holder at all; the memory store: comms and the console.
    assert ("comms", []) in minted
    assert ("lucent", sorted([compose.COMMS, compose.CONSOLE])) in minted


def test_the_sites_and_the_tools_hold_no_gateway_key_and_no_tools_token_and_the_tools_are_told_the_sites_address(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=["console", "terminal", "tools"]))
    home = inst.home
    generated = {n: f"v-{n}-0123456789" for n in compose.secret_names(inst.answers, True)}
    compose.env_values(home, inst.answers, generated, {}, extra={"MIND_PROXY_KEY": "the-proxy-key-0123456789"})
    services = compose.render(home, inst.answers, inst.host).compose["services"]

    for name in (compose.CONSOLE, compose.TERMINAL, "hive-tools"):
        environment = services[name]["environment"]
        assert "COMMS_BEARER_TOKEN" not in environment, name
        assert "HIVE_TOOLS_TOKEN" not in environment, name
    assert "COMMS_BEARER_TOKEN" not in services["wren"]["environment"]
    assert "HIVE_TOOLS_TOKEN" not in services["wren"]["environment"]
    # The tools put a link a person can open in an approval notice.
    public = services["hive-tools"]["environment"]["HIVE_SITE_PUBLIC_URL"]
    assert public == f"http://{inst.host.address}:{compose.CONSOLE_PORT}"
    assert compose.service_holders(inst.answers)["comms"] == []
    assert "COMMS_TOKEN_CONSOLE" not in generated and not any(
        n.startswith("COMMS_TOKEN") for n in compose.secret_names(inst.answers, True))
