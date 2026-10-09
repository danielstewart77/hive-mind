"""Every credential in the hive, one row per holder, with what it is doing.

`credentials_data` is the writer and has its own suite. These guard the reader
the page is built from: who holds what, what a live check found, and — the
whole point of the states — that a service which did not answer is never
rendered as a credential that was refused.
"""

import os
import sys
import time

import asyncio

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import credential_registry as registry
import credentials_data


@pytest.fixture(autouse=True)
def forget():
    registry.forget_all()
    yield
    registry.forget_all()


def _write_env(path, **kv):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n")


def _patch_env_files(monkeypatch, tmp_path, **files):
    env_files = {
        label: tmp_path / f"missing-{label}.env" for label in credentials_data.ENV_FILES
    }
    env_files.update(files)
    monkeypatch.setattr(credentials_data, "ENV_FILES", env_files)
    monkeypatch.setattr(credentials_data, "GIT_CREDENTIALS_PATH", tmp_path / "nogit")
    monkeypatch.setattr(credentials_data, "GH_HOSTS_PATH", tmp_path / "nohosts")


def _mind(name, gateway="http://mind:8420", last_seen=None):
    return {
        "name": name,
        "id": f"{name}-uuid",
        "gateway_url": gateway,
        "last_seen": time.time() if last_seen is None else last_seen,
    }


def _run(coro):
    """This repo has no pytest-asyncio; `asyncio.run` is its convention."""
    return asyncio.run(coro)


def _transport(handler):
    """A stand-in for the network. The transport is where our process ends."""
    return httpx.MockTransport(handler)


@pytest.fixture()
def network(monkeypatch):
    """Route every probe through one handler, keyed on the request."""
    routes: dict[str, object] = {}

    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = _transport(lambda request: _answer(request, routes))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return routes


def _answer(request, routes):
    for fragment, outcome in routes.items():
        if fragment in str(request.url):
            if isinstance(outcome, Exception):
                raise outcome
            status, body = outcome
            return httpx.Response(status, json=body)
    return httpx.Response(599, json={})


class TestWhoHoldsWhat:
    def test_every_registered_mind_gets_a_row_for_a_per_mind_credential(
        self, tmp_path, monkeypatch
    ) -> None:
        """Including a mind that has never been given one: "not set" is the
        thing the operator came here to find, and a mind missing from the
        page cannot report it."""
        _patch_env_files(monkeypatch, tmp_path)

        rows = registry.build_rows([_mind("cypher"), _mind("bilby")])

        holders = {
            row["holder"]
            for row in rows
            if row["credential"] == "mind_github_token"
        }
        assert holders == {"cypher", "bilby"}

    def test_a_shared_secret_gets_one_row_per_file_that_carries_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """A row per mind would be four identical rows backed by one `.env`
        that no row could ever report independently."""
        _patch_env_files(monkeypatch, tmp_path)

        rows = [
            row
            for row in registry.build_rows([])
            if row["credential"] == "comms_bearer_token"
        ]

        declared = next(
            g for g in credentials_data.CREDENTIAL_GROUPS
            if g["key"] == "comms_bearer_token"
        )["locations"]
        assert [row["holder"] for row in rows] == [label for label, _ in declared]

    def test_rotation_belongs_to_the_credential_and_not_to_any_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """Rotating one holder's copy of a shared secret and not the rest is
        how the hive goes down."""
        _patch_env_files(monkeypatch, tmp_path)

        rows = registry.build_rows([_mind("cypher")])
        summaries = registry.credential_summaries([_mind("cypher")])

        assert all("rotatable" not in row for row in rows)
        comms = next(s for s in summaries if s["key"] == "comms_bearer_token")
        assert comms["rotatable"] is True

    def test_a_per_mind_credential_offers_no_rotation_at_all(
        self, tmp_path, monkeypatch
    ) -> None:
        """There is no one value to fan out, so nothing a single rotation
        here could mean."""
        _patch_env_files(monkeypatch, tmp_path)

        summaries = registry.credential_summaries([_mind("cypher")])

        token = next(s for s in summaries if s["key"] == "mind_github_token")
        assert (token["rotatable"], token["settable"]) == (False, True)

    def test_a_row_says_where_the_credential_is_kept(
        self, tmp_path, monkeypatch
    ) -> None:
        """Which file and which key is the operator's whole remedy path."""
        nervous = tmp_path / "ns.env"
        _write_env(nervous, COMMS_BEARER_TOKEN="abcdwxyz")
        _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous)

        row = next(
            r
            for r in registry.build_rows([])
            if r["credential"] == "comms_bearer_token"
            and r["holder"] == "nervous_system"
        )

        assert str(nervous) in row["where"]
        assert "COMMS_BEARER_TOKEN" in row["where"]

    def test_a_present_value_shows_only_its_last_four_characters(
        self, tmp_path, monkeypatch
    ) -> None:
        hive_mind = tmp_path / "hm.env"
        _write_env(hive_mind, COMMS_BEARER_TOKEN="aaaabbbbccccwxyz")
        _patch_env_files(monkeypatch, tmp_path, hive_mind=hive_mind)

        row = next(
            r
            for r in registry.build_rows([])
            if r["credential"] == "comms_bearer_token" and r["holder"] == "hive_mind"
        )

        assert row["preview"] == "...wxyz"
        assert row["present"] is True

    def test_the_rows_come_back_in_a_stable_order(
        self, tmp_path, monkeypatch
    ) -> None:
        """The table is sortable: an order that varied between loads would
        reshuffle rows under the sort the operator just chose."""
        _patch_env_files(monkeypatch, tmp_path)
        minds = [_mind("nagatha"), _mind("ada"), _mind("cypher")]

        first = registry.build_rows(minds)
        second = registry.build_rows(list(reversed(minds)))

        key = lambda rows: [(r["credential"], r["holder"]) for r in rows]  # noqa: E731
        assert key(first) == key(second)


class TestWhatIsKnownBeforeAnyCheck:
    def test_the_listing_renders_rows_with_no_check_taken(
        self, tmp_path, monkeypatch, network
    ) -> None:
        """This is what the operator sees first; it must not wait on a mind
        that is down or on GitHub once per row."""
        nervous = tmp_path / "ns.env"
        _write_env(nervous, COMMS_BEARER_TOKEN="aaaawxyz")
        _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous)
        network["anything"] = httpx.ConnectError("nothing is reachable")

        rows = registry.build_rows([_mind("cypher")])

        mind_row = next(
            r for r in rows if r["credential"] == "mind_github_token"
        )
        assert mind_row["status"] == registry.UNCHECKED
        assert mind_row["holder"] == "cypher"
        assert mind_row["age_seconds"] is None

    def test_an_unchecked_mind_row_is_not_reported_as_unset(
        self, tmp_path, monkeypatch
    ) -> None:
        """Presence here is the mind's own answer. "Not set" is a sentence the
        operator acts on by pasting a token that was never missing."""
        _patch_env_files(monkeypatch, tmp_path)

        row = next(
            r
            for r in registry.build_rows([_mind("cypher")])
            if r["credential"] == "mind_github_token"
        )

        assert row["present"] is None
        assert row["status"] != registry.NOT_SET

    def test_a_mind_registered_with_no_address_is_its_own_state(
        self, tmp_path, monkeypatch
    ) -> None:
        """The remedy is a registry entry, not a trip to the machine."""
        _patch_env_files(monkeypatch, tmp_path)

        row = next(
            r
            for r in registry.build_rows([_mind("hex", gateway="")])
            if r["credential"] == "mind_github_token"
        )

        assert row["status"] == registry.UNADDRESSABLE
        assert row["checkable"] is False

    def test_a_mind_not_seen_for_a_week_reads_as_retired(
        self, tmp_path, monkeypatch
    ) -> None:
        """Broker rows are an upsert with no delete, so a decommissioned box
        stays registered — and a page whose steady state is a block of
        unreachable rows is one where a live mind falling in is invisible."""
        _patch_env_files(monkeypatch, tmp_path)
        gone = time.time() - registry.RETIRED_AFTER_S - 60

        row = next(
            r
            for r in registry.build_rows([_mind("zack", last_seen=gone)])
            if r["credential"] == "mind_github_token"
        )

        assert row["status"] == registry.RETIRED
        assert row["checkable"] is False

    def test_the_holder_that_validates_a_secret_is_never_probed_for_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """It is the thing the others authenticate to; a probe would be the
        console asking a service about itself."""
        nervous = tmp_path / "ns.env"
        _write_env(nervous, COMMS_BEARER_TOKEN="aaaawxyz")
        _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous)

        row = next(
            r
            for r in registry.build_rows([])
            if r["credential"] == "comms_bearer_token"
            and r["holder"] == "nervous_system"
        )

        assert row["status"] == registry.VALIDATOR
        assert row["checkable"] is False


class TestWhatALiveCheckFinds:
    def test_accepted_refused_and_unreachable_are_three_answers(
        self, tmp_path, monkeypatch, network
    ) -> None:
        _patch_env_files(monkeypatch, tmp_path)
        minds = [
            _mind("good", gateway="http://good:8420"),
            _mind("bad", gateway="http://bad:8420"),
            _mind("gone", gateway="http://gone:8420"),
        ]
        monkeypatch.setattr(
            "mind_runtime.admin_headers", lambda: {"Authorization": "Bearer a"}
        )
        network["good:8420"] = (200, {"stored": True, "accepted": True, "login": "dan"})
        network["bad:8420"] = (200, {"stored": True, "accepted": False, "detail": "no"})
        network["gone:8420"] = httpx.ConnectError("no route")

        answers = {
            m["name"]: (_run(registry.check("mind_github_token", m["name"], minds)))[
                "status"
            ]
            for m in minds
        }

        assert answers == {
            "good": registry.WORKING,
            "bad": registry.REFUSED,
            "gone": registry.UNREACHABLE,
        }

    def test_a_working_token_is_named_by_the_account_it_belongs_to(
        self, tmp_path, monkeypatch, network
    ) -> None:
        _patch_env_files(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "mind_runtime.admin_headers", lambda: {"Authorization": "Bearer a"}
        )
        network["cypher:8420"] = (
            200,
            {
                "stored": True,
                "accepted": True,
                "login": "danielstewart77",
                "where": "keyring:CYPHER_GITHUB_TOKEN",
            },
        )

        answer = _run(registry.check(
            "mind_github_token", "cypher", [_mind("cypher", "http://cypher:8420")]
        ))

        assert answer["names"] == "danielstewart77"
        assert answer["where"] == "keyring:CYPHER_GITHUB_TOKEN"

    def test_a_mind_without_the_route_is_not_a_mind_that_is_down(
        self, tmp_path, monkeypatch, network
    ) -> None:
        """One remedy is a redeploy and the other is a trip to the machine."""
        _patch_env_files(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "mind_runtime.admin_headers", lambda: {"Authorization": "Bearer a"}
        )
        network["old:8420"] = (404, {})
        network["down:8420"] = httpx.ConnectTimeout("timed out")
        minds = [_mind("old", "http://old:8420"), _mind("down", "http://down:8420")]

        old = _run(registry.check("mind_github_token", "old", minds))
        down = _run(registry.check("mind_github_token", "down", minds))

        assert old["status"] == registry.PREDATES_API
        assert down["status"] == registry.UNREACHABLE

    def test_a_mind_refusing_this_consoles_credential_is_not_a_bad_token(
        self, tmp_path, monkeypatch, network
    ) -> None:
        """The stored token was never judged. Reporting it refused would send
        the operator off to revoke one that is fine."""
        _patch_env_files(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "mind_runtime.admin_headers", lambda: {"Authorization": "Bearer a"}
        )
        network["cypher:8420"] = (401, {"error": "unauthorized"})

        answer = _run(registry.check(
            "mind_github_token", "cypher", [_mind("cypher", "http://cypher:8420")]
        ))

        assert answer["status"] == registry.UNDETERMINED

    def test_a_console_with_no_admin_token_says_so_rather_than_blaming_the_mind(
        self, tmp_path, monkeypatch, network
    ) -> None:
        _patch_env_files(monkeypatch, tmp_path)

        def _refuse():
            raise RuntimeError("no admin token configured")

        monkeypatch.setattr("mind_runtime.admin_headers", _refuse)

        answer = _run(registry.check(
            "mind_github_token", "cypher", [_mind("cypher", "http://cypher:8420")]
        ))

        assert answer["status"] == registry.UNDETERMINED
        assert "console" in answer["detail"]

    def test_a_shared_secret_is_presented_to_the_service_that_validates_it(
        self, tmp_path, monkeypatch
    ) -> None:
        hive_mind = tmp_path / "hm.env"
        _write_env(hive_mind, COMMS_BEARER_TOKEN="the-live-value")
        _patch_env_files(monkeypatch, tmp_path, hive_mind=hive_mind)
        seen: list[str] = []

        def handler(request):
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json=[])

        original = httpx.AsyncClient

        class _Client(original):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        answer = _run(registry.check("comms_bearer_token", "hive_mind", []))

        assert answer["status"] == registry.WORKING
        assert seen == ["Bearer the-live-value"]

    def test_a_validator_that_rejects_the_value_is_a_refusal(
        self, tmp_path, monkeypatch, network
    ) -> None:
        hive_mind = tmp_path / "hm.env"
        _write_env(hive_mind, COMMS_BEARER_TOKEN="stale-value")
        _patch_env_files(monkeypatch, tmp_path, hive_mind=hive_mind)
        network["broker/minds"] = (401, {})

        answer = _run(registry.check("comms_bearer_token", "hive_mind", []))

        assert answer["status"] == registry.REFUSED

    def test_a_validator_that_did_not_answer_is_never_a_refusal(
        self, tmp_path, monkeypatch, network
    ) -> None:
        """An outage, a 500, a secondary rate limit — the remedy a refusal
        suggests is revoke and regenerate, applied to a credential that
        works."""
        hive_mind = tmp_path / "hm.env"
        _write_env(hive_mind, COMMS_BEARER_TOKEN="fine-value")
        _patch_env_files(monkeypatch, tmp_path, hive_mind=hive_mind)
        network["broker/minds"] = (503, {})

        answer = _run(registry.check("comms_bearer_token", "hive_mind", []))

        assert answer["status"] == registry.UNDETERMINED

    def test_an_unreachable_validator_is_not_a_refusal_either(
        self, tmp_path, monkeypatch, network
    ) -> None:
        hive_mind = tmp_path / "hm.env"
        _write_env(hive_mind, COMMS_BEARER_TOKEN="fine-value")
        _patch_env_files(monkeypatch, tmp_path, hive_mind=hive_mind)
        network["broker/minds"] = httpx.ConnectError("down")

        answer = _run(registry.check("comms_bearer_token", "hive_mind", []))

        assert answer["status"] == registry.UNDETERMINED


class TestHowOldAnAnswerIs:
    def test_a_remembered_check_is_reported_with_its_age(self) -> None:
        """A status with no age beside it reads as current, and this one may
        be minutes old by design."""
        registry.remember("mind_github_token", "cypher", status=registry.WORKING)

        entry = registry.recall("mind_github_token", "cypher")

        assert entry.status == registry.WORKING
        assert registry._checked_fields(entry)["age_seconds"] >= 0

    def test_an_answer_past_its_window_is_marked_stale(self, monkeypatch) -> None:
        entry = registry.remember(
            "mind_github_token", "cypher", status=registry.WORKING
        )
        entry.monotonic_at -= registry.CHECK_TTL_S + 1

        assert registry._checked_fields(entry)["stale"] is True

    def test_a_remembered_answer_reaches_the_row(self, tmp_path, monkeypatch) -> None:
        _patch_env_files(monkeypatch, tmp_path)
        registry.remember(
            "mind_github_token", "cypher", status=registry.WORKING,
            present=True, names="danielstewart77",
            where="keyring:CYPHER_GITHUB_TOKEN",
        )

        row = next(
            r
            for r in registry.build_rows([_mind("cypher")])
            if r["credential"] == "mind_github_token"
        )

        assert row["status"] == registry.WORKING
        assert row["names"] == "danielstewart77"
        assert row["where"] == "keyring:CYPHER_GITHUB_TOKEN"
