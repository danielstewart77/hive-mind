"""The per-turn hook asks about every name it found in one request.

The hook treats each capitalised word as a candidate person, and used to
issue one ``GET /graph/query?entity_name=X`` per candidate — a dozen serial
round trips on the hot path of every prompt, each putting a person's name in
a URL that Zeek, uvicorn and Loki all record verbatim.

The copy under test is ``minds/ada/.claude/hooks/contextual_retrieval.py`` —
the file a running mind executes, bind-mounted into its container. Hooks are
per-host installs by design and ``minds/*/`` is gitignored, so this skips on a
checkout with no mind installed and runs on every machine that has one. It is
loaded by path, not by module name: ``nervous-system/comms/contextual_retrieval.py``
answers to that same import name and has no person branch at all, so an
import-by-name here would assert nothing while reporting green.

Requirements under test:
  R2   several names cost one request, and that request names all of them
  R1   the address the hook calls carries no query string
  R9   a person named twice in one prompt is rendered once
  R5   a prompt full of capitalised words still yields a cue, and says how
       much of itself was not looked up
  R10  one unresolvable name does not suppress the names that resolved
  R3   a name resolving to something that is not a person is not rendered as
       one, which is what makes the capitalised-word heuristic safe
  R5b  a question after a long paste still gets its names looked up
  R10b a token too long to be a name is never sent
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HOOK = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "minds", "ada", ".claude", "hooks", "contextual_retrieval.py",
    )
)


pytestmark = pytest.mark.skipif(
    not os.path.isfile(HOOK), reason="no mind installed in this checkout"
)


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_ada_contextual_retrieval", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _person(name: str, first: str = "") -> dict:
    props = {"name": name, "type": "Person"}
    if first:
        props["first_name"] = first
    return {"properties": props, "connections": []}


class _Lucent:
    """A stub graph that answers a batch, recording what it was asked."""

    def __init__(self, nodes: dict[str, dict], counts: dict[str, int] | None = None):
        self.nodes = nodes
        self.counts = counts or {}
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()
                )
                outer.requests.append({"path": self.path, "body": body})
                results = []
                for n in body.get("names", []):
                    node = outer.nodes.get(n)
                    count = outer.counts.get(n, 1 if node else 0)
                    results.append({
                        "entity": n,
                        "found": node is not None,
                        "count": count,
                        "matches": [node] if node else [],
                    })
                payload = json.dumps({"results": results}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                # The form this change deleted. Answering it would let an
                # unmigrated hook pass this suite.
                self.send_error(405)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# ---- R2 / R1 ----


def test_r2_several_names_cost_one_request_naming_all_of_them(hook):
    nodes = {"Maurice": _person("Maurice Westerdale"), "Corey": _person("Corey Means")}
    with _Lucent(nodes) as lucent:
        block = hook._known_persons_block(
            lucent.url, {}, "Maurice and Corey met at Anchor Bend"
        )
    assert len(lucent.requests) == 1
    asked = lucent.requests[0]["body"]["names"]
    assert "Maurice" in asked and "Corey" in asked and "Anchor" in asked
    assert "Maurice Westerdale" in block and "Corey Means" in block


def test_r1_the_address_the_hook_calls_carries_no_query_string(hook):
    with _Lucent({"Maurice": _person("Maurice Westerdale")}) as lucent:
        hook._known_persons_block(lucent.url, {}, "what about Maurice")
    assert lucent.requests[0]["path"] == "/graph/query"


# ---- R9 ----


def test_r9_a_person_named_twice_is_rendered_once(hook):
    node = _person("Maurice Westerdale", first="Maurice")
    with _Lucent({"Maurice": node, "Westerdale": node}) as lucent:
        block = hook._known_persons_block(lucent.url, {}, "Maurice Westerdale called")
    assert block.count("Maurice Westerdale (Person)") == 1


# ---- R5 ----


def test_r5_a_prompt_full_of_capitalised_words_still_yields_a_cue(hook):
    # The extractor tokenizes on letters only, so the filler has to be
    # alphabetic to count as 500 distinct candidates.
    import string

    filler = [
        f"W{a}{b}"
        for a in string.ascii_lowercase
        for b in string.ascii_lowercase
    ][:500]  # 500 fillers + "Maurice" = 501 distinct candidates
    prompt = "Maurice " + " ".join(filler)
    with _Lucent({"Maurice": _person("Maurice Westerdale")}) as lucent:
        block = hook._known_persons_block(lucent.url, {}, prompt)
    assert len(lucent.requests) == 1
    assert len(lucent.requests[0]["body"]["names"]) == 256
    assert "Maurice Westerdale" in block
    # 501 distinct words, less "Who" and "Why" which the stopword list drops
    # before any of this, leaves 499 candidates. 256 are asked about, so 243
    # go unlooked-up and the block says so rather than presenting a partial
    # list as if it were the whole.
    assert "243 further capitalised words" in block


# ---- R10 ----


def test_r10_an_ambiguous_name_does_not_suppress_the_names_that_resolved(hook):
    nodes = {"Maurice": _person("Maurice Westerdale"), "Stewart": _person("A Stewart")}
    with _Lucent(nodes, counts={"Stewart": 10}) as lucent:
        block = hook._known_persons_block(lucent.url, {}, "Maurice knows Stewart")
    assert "Maurice Westerdale" in block
    assert "A Stewart" not in block


def test_r3_a_name_that_is_not_a_person_is_not_rendered_as_one(hook):
    church = {"properties": {"name": "Anchor Bend", "type": "Organization"},
              "connections": []}
    nodes = {"Maurice": _person("Maurice Westerdale"), "Anchor": church}
    with _Lucent(nodes) as lucent:
        block = hook._known_persons_block(lucent.url, {}, "Maurice at Anchor Bend")
    assert "Maurice Westerdale" in block
    assert "Anchor Bend" not in block


def _filler(n: int) -> str:
    import string

    words = [f"W{a}{b}" for a in string.ascii_lowercase for b in string.ascii_lowercase]
    return " ".join(words[:n])


def test_r5b_a_question_after_a_long_paste_still_gets_its_names_looked_up(hook):
    """Taking the first N candidates drops the ones the question carries.

    Measured live before this: a pasted module followed by "what did Maurice
    say" came back with Maurice missing and a name from inside the paste
    present, which reads as "the graph was consulted" while being wrong.
    """
    prompt = _filler(500) + " . So what did Maurice say about it?"
    with _Lucent({"Maurice": _person("Maurice Westerdale")}) as lucent:
        block = hook._known_persons_block(lucent.url, {}, prompt)
    assert "Maurice" in lucent.requests[0]["body"]["names"]
    assert "Maurice Westerdale" in block


def test_r10b_a_token_too_long_to_be_a_name_is_never_sent(hook):
    prompt = "Ask Maurice about " + "X" + "x" * 200 + " today"  # 201 chars
    with _Lucent({"Maurice": _person("Maurice Westerdale")}) as lucent:
        block = hook._known_persons_block(lucent.url, {}, prompt)
    asked = lucent.requests[0]["body"]["names"]
    assert all(len(n) <= 200 for n in asked)
    assert "Maurice Westerdale" in block
