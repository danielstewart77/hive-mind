"""The Stop hook's person branch reads the batched answer correctly.

`auto_remember.sh` decides whether to attach an observation to a person by
reading a count and a node type out of lucent's reply with jq. It is bash, so
there is no function to import — but the two jq filters are the contract
between the script and the route, and both ends of that contract are live
here: the filters are read out of the script file, and the JSON is whatever
the real route returns.

A wrong filter is silent. The branch takes its else arm, the observation is
discarded, and the log blames the name rather than the shape.

The branch itself is extracted from the script and executed against a
recording ``curl``, because reading the filters alone proves nothing about
what the script sends: a revert to the old ``-G`` form leaves both filters
untouched and discards every person observation there is.

Requirements under test:
  R1  the name is in the request body; the address carries no query string
  R7  the Stop hook's person branch still resolves a name through the body route
  R3  an ambiguous name is still seen as ambiguous, so an observation is never
      attached to whichever person happened to sort first
  R8  a graph it cannot reach is logged as unreachable, not as a name that did
      not resolve — curl exits 0 on a 404, so without this the observation is
      destroyed under a reason that sends its reader into the knowledge graph
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

HOOK = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "minds", "ada", ".claude", "hooks", "auto_remember.sh",
    )
)

# Hooks are per-host installs and ``minds/*/`` is gitignored: this runs on
# every machine with a mind on it and skips on a bare checkout.
pytestmark = [
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed"),
    pytest.mark.skipif(
        not os.path.isfile(HOOK), reason="no mind installed in this checkout"
    ),
]

pytest.importorskip("hive_data")

from hive_data import graph as hd_graph  # noqa: E402

from lucent_api.tests.conftest import unique  # noqa: E402


def _filter(var: str) -> str:
    """Read the jq filter the script actually runs for PERSON_COUNT / PERSON_TYPE."""
    src = open(HOOK, encoding="utf-8").read()
    m = re.search(rf"""{var}=\$\(printf '%s' "\$PERSON_RESP" \| jq -r '([^']+)'""", src)
    assert m, f"{var} filter not found in {HOOK}"
    return m.group(1)


def _jq(filt: str, payload: dict) -> str:
    out = subprocess.run(
        ["jq", "-r", filt], input=json.dumps(payload), capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


#: About the person branch of the graph query, not about scoping, so it
#: presents the admin credential — the one value that still reaches every
#: owner's graph. It used to clear the token instead and rely on the memory
#: store serving open when unconfigured, which is gone: a store that cannot
#: establish who is calling refuses rather than answering the LAN. Without a
#: credential the query returns a 401 body, whose person count is zero, and
#: the failure reads as the person branch being broken.
ADMIN_TOKEN = "lucent-admin-for-tests"  # secret-guard: allow


@pytest_asyncio.fixture
async def client(store, monkeypatch):
    """The real app over ASGI, on this test's own event loop.

    `TestClient` would run the routes in a portal thread with a loop of its
    own, and the pool's connections belong to the loop that opened them.
    """
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)

    from lucent_api.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://lucent",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as opened:
        yield opened


@pytest_asyncio.fixture
async def people(client, registered_types, a_mind) -> dict:
    """One name that resolves to a person, and one shared by two of them.

    Both are minted with `unique()`: the scratch database is shared by the
    whole run, so a fixed surname would make "how many people answer to
    Stewart" depend on which other test had already written one. Not a
    generator — `hive_data` refuses to open a unit of work inside one, and a
    `node_write` is a unit.
    """
    owner = unique("daniel")
    surname = unique("Stewart")
    alone = unique("Maurice")
    for first, last in ((alone, unique("Westerdale")),
                        (unique("Daniel"), surname),
                        (unique("Sloan"), surname)):
        await hd_graph.node_write(
            owner=owner, type="Person", name=f"{first} {last}",
            first_name=first, last_name=last, mind_id=a_mind, properties={},
            data_class="current-state", tier="contextual",
            created_at=1.0, updated_at=1.0)
    return {"alone": alone, "shared": surname}


async def _query(client: httpx.AsyncClient, name: str) -> dict:
    answer = await client.post(
        "/graph/query", json={"names": [name], "depth": 1})
    assert answer.status_code == 200, answer.text
    return answer.json()


@pytest.mark.asyncio
async def test_r7_the_person_branch_resolves_a_single_person(client, people):
    payload = await _query(client, people["alone"])

    assert _jq(_filter("PERSON_COUNT"), payload) == "1"
    assert _jq(_filter("PERSON_TYPE"), payload) == "Person"


@pytest.mark.asyncio
async def test_r3_the_person_branch_still_sees_an_ambiguous_name_as_ambiguous(
    client, people
):
    payload = await _query(client, people["shared"])

    assert _jq(_filter("PERSON_COUNT"), payload) == "2"


# ---- the branch itself, run against a recording curl ----


def _person_branch() -> str:
    """The lines the script really runs, from the script itself."""
    src = open(HOOK, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(src) if l.strip().startswith("PERSON_RESP=$(curl"))
    # Walk back over the comment block that introduces it, forward to the
    # `fi` closing the branch at the same indentation.
    indent = len(src[start]) - len(src[start].lstrip())
    end = next(
        i for i in range(start, len(src))
        if src[i] == " " * indent + "fi"
    )
    return "\n".join(src[start:end + 1])


def _run_branch(tmp_path, *, reply: str, curl_exit: int = 0) -> dict:
    """Execute the branch with a curl that records its arguments."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv"
    fake_curl = bindir / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        f"if [ {curl_exit} -ne 0 ]; then exit {curl_exit}; fi\n"
        f"cat <<'JSON'\n{reply}\nJSON\n"
    )
    fake_curl.chmod(0o755)

    out = tmp_path / "out"
    script = f"""
_store() {{ printf 'STORE %s|%s|%s\\n' "$1" "$2" "$3" >> "{out}"; }}
_log() {{ printf 'LOG %s\\n' "$1" >> "{out}"; }}
P_NAME='Stewart'
P_PROSE='has opinions about hyphens'
ISO_NOW='2026-09-15T21:00:00Z'
SESSION_ID='sid'
LUCENT_AUTH='Authorization: Bearer token'
LUCENT_URL='http://lucent.invalid'
{_person_branch()}
"""
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return {
        "out": out.read_text() if out.exists() else "",
        "argv": argv_log.read_text() if argv_log.exists() else "",
    }


_RESOLVED = json.dumps({
    "results": [{
        "entity": "Stewart", "found": True, "count": 1,
        "matches": [{"properties": {"name": "Daniel Stewart", "type": "Person"}}],
    }]
})
_AMBIGUOUS = json.dumps({
    "results": [{
        "entity": "Stewart", "found": True, "count": 2,
        "matches": [{"properties": {"name": "Daniel Stewart", "type": "Person"}}],
    }]
})


def test_r1_the_branch_sends_the_name_in_a_body_to_an_address_with_no_query(tmp_path):
    argv = _run_branch(tmp_path, reply=_RESOLVED)["argv"]
    assert "-X POST" in argv
    assert "Stewart" in argv
    assert "/graph/query" in argv
    assert "entity_name" not in argv
    assert "/graph/query?" not in argv


def test_r7_a_name_resolving_to_one_person_attaches_the_observation(tmp_path):
    out = _run_branch(tmp_path, reply=_RESOLVED)["out"]
    assert "STORE current-state|[About Stewart] has opinions about hyphens|person" in out


def test_r3_an_ambiguous_name_attaches_nothing(tmp_path):
    out = _run_branch(tmp_path, reply=_AMBIGUOUS)["out"]
    assert "STORE" not in out
    assert "name-unresolved" in out


def test_r8_an_unreachable_graph_is_logged_as_unreachable_not_as_an_unknown_name(
    tmp_path,
):
    out = _run_branch(tmp_path, reply="", curl_exit=22)["out"]
    assert "graph-unreachable" in out
    assert "name-unresolved" not in out
    assert "STORE" not in out
