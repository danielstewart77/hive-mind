"""Ask the running hive whether it is actually working on Postgres.

Fired after the cutover, from outside: every check goes through a service's
own HTTP surface and then looks in Postgres for what that call should have
written. A read-only check cannot tell a service reading Postgres from a
service still reading its SQLite file — both answer correctly — so every
area here writes something and then finds it in the new database.

Reported per app, pass or fail. An app this host does not run is reported as
absent rather than failing: a mind's workstation runs the gateway and the
memory store, a corporate target may run neither.

    python -m unified_db.postflight

Exit 0 only when every app present passed.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg


@dataclass
class AppResult:
    app: str
    state: str          # "ok", "fail", "absent"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"


@dataclass
class Postflight:
    """Every app, and whether the set of them answers the question.

    `absent` is its own state and does **not** count as a pass. An earlier
    version folded it into one: a service that was not installed, a service
    with no data to ask about, and a service that was simply *down* all
    reported absent, and absent made the whole report green. Three of those
    have different remedies and one of them is an outage.
    """

    apps: list[AppResult] = field(default_factory=list)

    @property
    def passing(self) -> list[AppResult]:
        return [a for a in self.apps if a.state == "ok"]

    @property
    def failing(self) -> list[AppResult]:
        return [a for a in self.apps if a.state == "fail"]

    @property
    def absent(self) -> list[AppResult]:
        return [a for a in self.apps if a.state == "absent"]

    @property
    def ok(self) -> bool:
        """Every app present passed, and at least one did."""
        return bool(self.passing) and not self.failing


def _call(method: str, url: str, token: str | None = None,
          body: dict | None = None, timeout: float = 20.0) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode() or "{}"
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:200]}


def _items(answer: object, key: str) -> list:
    """The list inside an answer, however the service wrapped it.

    These five services were written by different hands and some answer a
    bare JSON array where others answer an object with the array under a
    key. A check that assumed one shape would report a working app as
    broken, which is the one thing a cutover check must not do.
    """
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict):
        value = answer.get(key)
        if isinstance(value, list):
            return value
    return []


class Absent(RuntimeError):
    """The app is not running here, which is not a failure."""


#: Where the probe records what it overwrote, before it overwrites it.
#: In the working directory rather than a temporary one: a file that is swept
#: away is no use to the operator holding a marker in a live column.
JOURNAL_VAR = "POSTFLIGHT_JOURNAL"


def journal_path() -> Path:
    return Path(os.environ.get(JOURNAL_VAR)
                or Path.cwd() / "postflight-probe.json")


class _Probe:
    """Change one value in Postgres, so a read through the app can prove it.

    A read-only comparison cannot tell a service reading Postgres from a
    service still reading its SQLite file: the loader made the two agree, so
    both answer correctly. The only question that separates them is one whose
    answer exists in exactly one of the stores — so this writes a marker into
    Postgres, lets the check read the app, and puts the original value back.

    What it overwrote goes to a file *before* the write, because the three
    columns it marks are all `NOT NULL` and load-bearing: a mind whose
    `model` is a name the proxy does not list has every spawn refused. Held
    only in process memory, a kill inside the probe window — and the window
    is an HTTP call whose timeout is per-socket-operation rather than a
    deadline, so a service dribbling bytes holds it open — leaves a marker in
    a live column with nothing on disk saying what the value was. A journal
    left behind refuses the next run rather than being cleaned up quietly,
    since the marker is still in the database.
    """

    def __init__(self, conn: asyncpg.Connection, table: str, column: str,
                 key: str, key_value: object, marker: str):
        self._conn = conn
        self._table = table
        self._column = column
        self._key = key
        self._key_value = key_value
        self._marker = marker
        self._original: object = None

    async def __aenter__(self) -> str:
        self._original = await self._conn.fetchval(
            f'SELECT "{self._column}" FROM {self._table} '
            f'WHERE "{self._key}" = $1', self._key_value)
        if self._original is None:
            raise Absent(
                f"{self._table}.{self._column} is null on the row this probe "
                "would mark; restoring it would fail and leave the marker")
        record = {
            "table": self._table,
            "column": self._column,
            "key": self._key,
            "key_value": self._key_value,
            "original": self._original,
            "marker": self._marker,
        }
        path = journal_path()
        path.write_text(json.dumps(record, indent=2, default=str))
        await self._conn.execute(
            f'UPDATE {self._table} SET "{self._column}" = $1 '
            f'WHERE "{self._key}" = $2', self._marker, self._key_value)
        return self._marker

    async def __aexit__(self, *_exc: object) -> None:
        await self._conn.execute(
            f'UPDATE {self._table} SET "{self._column}" = $1 '
            f'WHERE "{self._key}" = $2', self._original, self._key_value)
        journal_path().unlink(missing_ok=True)


async def restore_journal(conn: asyncpg.Connection) -> str:
    """Put back what a killed run left marked, and say what it was."""
    path = journal_path()
    record = json.loads(path.read_text())
    await conn.execute(
        f'UPDATE {record["table"]} SET "{record["column"]}" = $1 '
        f'WHERE "{record["key"]}" = $2',
        record["original"], record["key_value"])
    path.unlink(missing_ok=True)
    return (f'{record["table"]}.{record["column"]} restored to '
            f'{record["original"]!r} from {path}')


def _items(answer: object, key: str) -> list:
    """The list inside an answer, however the service wrapped it.

    These five services were written by different hands and some answer a
    bare JSON array where others answer an object with the array under a
    key. A check that assumed one shape would report a working app as
    broken, which is the one thing a cutover check must not do.
    """
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict):
        value = answer.get(key)
        if isinstance(value, list):
            return value
    return []


class Absent(RuntimeError):
    """The app is not running here, which is not a failure."""


class _Probe:
    """Change one value in Postgres, so a read through the app can prove it.

    A read-only comparison cannot tell a service reading Postgres from a
    service still reading its SQLite file: the loader made the two agree, so
    both answer correctly. The only question that separates them is one whose
    answer exists in exactly one of the stores — so this writes a marker into
    Postgres, lets the check read the app, and puts the original value back
    whatever happens.
    """

    def __init__(self, conn: asyncpg.Connection, table: str, column: str,
                 key: str, key_value: object, marker: str):
        self._conn = conn
        self._table = table
        self._column = column
        self._key = key
        self._key_value = key_value
        self._marker = marker
        self._original: object = None

    async def __aenter__(self) -> str:
        self._original = await self._conn.fetchval(
            f'SELECT "{self._column}" FROM {self._table} '
            f'WHERE "{self._key}" = $1', self._key_value)
        await self._conn.execute(
            f'UPDATE {self._table} SET "{self._column}" = $1 '
            f'WHERE "{self._key}" = $2', self._marker, self._key_value)
        return self._marker

    async def __aexit__(self, *_exc: object) -> None:
        await self._conn.execute(
            f'UPDATE {self._table} SET "{self._column}" = $1 '
            f'WHERE "{self._key}" = $2', self._original, self._key_value)


class Down(RuntimeError):
    """The app is installed here and not answering. A failure, not an absence."""


def _reachable(url: str) -> None:
    """Answering at all is enough; silence is a failure.

    Not an `Absent`: "this host does not run that service" and "that service
    is down" are different sentences, and only one of them is somebody's
    afternoon.
    """
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"),
                               timeout=5)
    except urllib.error.HTTPError:
        return
    except (urllib.error.URLError, OSError) as exc:
        raise Down(f"{url} is not answering: {exc}") from exc


# ---------------------------------------------------------------------------
# One area per app, each writing through the app and reading from Postgres
# ---------------------------------------------------------------------------

async def _lucent(conn: asyncpg.Connection) -> tuple[str, str]:
    """Store a memory through lucent, then find that row in Postgres.

    The probe is deleted either way — a cutover check must not leave a
    memory behind for a mind to recall. `source` has to be one lucent
    accepts (`tool`); the class is a real one, because `ephemeral` is the
    hive's discard signal and lucent answers a 200 saying `stored: false`,
    which an earlier version of this check read as "the row is not in
    Postgres, so the service is still on SQLite". A refused write and a
    write that went to the wrong store are different sentences with
    different remedies, so the response is read before the database is.
    """
    base = os.environ.get("LUCENT_URL_SELF", "").rstrip("/")
    token = os.environ.get("LUCENT_BEARER_TOKEN", "")
    mind_id = os.environ.get("MIND_ID", "")
    if not base or not mind_id or not token:
        raise Absent("no LUCENT_URL_SELF, LUCENT_BEARER_TOKEN or MIND_ID in "
                     "the environment")
    _reachable(f"{base}/health")
    marker = f"postflight {uuid.uuid4()}"
    answer = _call("POST", f"{base}/memory/store", token, {
        "mind_id": mind_id,
        "content": marker,
        "data_class": "current-state",
        "tier": "contextual",
        "source": "tool",
    })
    if not isinstance(answer, dict) or not answer.get("stored"):
        detail = (answer.get("error") if isinstance(answer, dict)
                  else repr(answer))
        return "fail", f"lucent refused the write: {detail}"
    stored_id = answer.get("id")
    try:
        row = await conn.fetchrow(
            "SELECT id, content_search IS NOT NULL AS indexed "
            "FROM memory.memories WHERE content = $1", marker)
        if row is None:
            return "fail", (f"lucent stored the memory as id {stored_id} and "
                            "it is not in memory.memories — it is still "
                            "writing to SQLite")
        if not row["indexed"]:
            return "fail", f"memory {row['id']} landed with no search vector"
        return "ok", (f"stored through lucent, found as memory {row['id']}, "
                      "indexed, removed")
    finally:
        # Through lucent's own API, so the probe is removed from whichever
        # store it actually went to. Deleting from Postgres only would leave
        # the memory behind on exactly the path this check exists to
        # detect — a mind recalling `postflight <uuid>`, one per run.
        if stored_id is not None:
            try:
                _call("DELETE", f"{base}/memory/{stored_id}", token)
            except (urllib.error.HTTPError, urllib.error.URLError,
                    OSError) as exc:
                raise Down(
                    f"the probe memory {stored_id} could not be deleted "
                    f"through lucent: {exc}") from exc


async def _comms(conn: asyncpg.Connection) -> tuple[str, str]:
    """Read the gateway's mind registry, with a marker only Postgres holds."""
    base = os.environ.get("COMMS_URL", "").rstrip("/")
    token = os.environ.get("COMMS_ADMIN_BEARER_TOKEN") or \
        os.environ.get("COMMS_BEARER_TOKEN", "")
    if not base:
        raise Absent("no COMMS_URL in the environment")
    _reachable(f"{base}/health")
    row = await conn.fetchrow(
        "SELECT mind_id, name FROM core.minds ORDER BY mind_id LIMIT 1")
    if row is None:
        return "fail", "core.minds is empty"
    marker = f"postflight-{uuid.uuid4().hex[:8]}"
    async with _Probe(conn, "core.minds", "model", "mind_id",
                      row["mind_id"], marker):
        answer = _call("GET", f"{base}/broker/minds", token)
    listed = _items(answer, "minds")
    if not listed:
        return "fail", "the gateway listed no minds"
    # The gateway names the mind id `id` in its listing; `mind_id` is
    # accepted too so a rename there does not read as a database fault.
    served = {m.get("id", m.get("mind_id")): m.get("model") for m in listed
              if isinstance(m, dict)}
    rows = await conn.fetch("SELECT mind_id FROM core.minds")
    if set(served) != {r["mind_id"] for r in rows}:
        return "fail", ("the gateway lists different minds than core.minds; "
                        "it is reading another store")
    if served.get(row["mind_id"]) != marker:
        return "fail", (f"{row['name']}'s model read back as "
                        f"{served.get(row['mind_id'])!r} while core.minds "
                        f"held {marker!r} — the gateway is still on SQLite")
    return "ok", (f"{len(served)} mind(s); a marker written only into "
                  "core.minds came back through the gateway")


async def _console(conn: asyncpg.Connection) -> tuple[str, str]:
    """The console's own board read, with a marker only Postgres holds.

    The board API is guarded by a signed-in admin session rather than a
    bearer, so unless this install has issued a token there is no credential
    a check outside a browser can present — reported absent, because a check
    that cannot authenticate has learned nothing. When there *is* one, it
    marks a title in `board.work_items` and requires it back: comparing two
    counts the loader deliberately made equal would pass a console still
    reading its SQLite file, which is the one sentence this module exists to
    make impossible.
    """
    base = os.environ.get("HIVE_SITE_URL", "http://127.0.0.1:8710").rstrip("/")
    token = os.environ.get("HIVE_SITE_ADMIN_TOKEN", "")
    if not token:
        raise Absent("the console's board API is session-guarded and no "
                     f"{'HIVE_SITE_ADMIN_TOKEN'} is configured, so this app "
                     "is unchecked")
    _reachable(base)
    row = await conn.fetchrow(
        "SELECT id, title FROM board.work_items ORDER BY position LIMIT 1")
    if row is None:
        return "fail", ("board.work_items is empty, so there is nothing to "
                        "ask the console for; this app is unchecked")
    marker = f"postflight-{uuid.uuid4().hex[:8]}"
    async with _Probe(conn, "board.work_items", "title", "id",
                      row["id"], marker):
        answer = _call("GET", f"{base}/api/kanban/data", token)
    items = (_items(answer, "epics") + _items(answer, "features")
             + _items(answer, "stories"))
    if not items:
        return "fail", "the console's board read returned nothing"
    ids = {i.get("id") for i in items if isinstance(i, dict)}
    titles = {i.get("title") for i in items if isinstance(i, dict)}
    total = await conn.fetchval("SELECT count(*) FROM board.work_items")
    if len(ids) != total:
        return "fail", (f"the console serves {len(ids)} board item(s), "
                        f"board.work_items holds {total}")
    if marker not in titles:
        return "fail", (f"a board item retitled {marker!r} in "
                        "board.work_items did not come back from the "
                        "console — it is still on SQLite")
    return "ok", (f"{total} board item(s); a marker written only into "
                  "board.work_items came back through the console")


async def _tools(conn: asyncpg.Connection) -> tuple[str, str]:
    """One approval request, fetched through hive-tools with a marker.

    `/hitl/{id}` is the only data route the service exposes, which makes it
    the whole check: the id comes out of Postgres, `caller_mind` is marked
    there and nowhere else, and the answer has to carry the marker.
    """
    base = os.environ.get("HIVE_TOOLS_URL", "").rstrip("/")
    token = os.environ.get("HIVE_TOOLS_TOKEN", "")
    if not base:
        raise Absent("no HIVE_TOOLS_URL in the environment")
    _reachable(f"{base}/health")
    row = await conn.fetchrow(
        "SELECT id, tool_name, status, caller_mind FROM tools.hitl_requests "
        "ORDER BY id DESC LIMIT 1")
    if row is None:
        return "fail", ("tools.hitl_requests is empty, so there is nothing "
                        "to ask hive-tools for; this app is unchecked")
    marker = f"postflight-{uuid.uuid4().hex[:8]}"
    async with _Probe(conn, "tools.hitl_requests", "caller_mind", "id",
                      row["id"], marker):
        answer = _call("GET", f"{base}/hitl/{row['id']}", token)
    if not isinstance(answer, dict):
        return "fail", f"hive-tools answered {answer!r}"
    served = answer.get("request", answer)
    if not isinstance(served, dict):
        return "fail", f"hive-tools answered {answer!r}"
    # The service names it `hitl_request_id`; `id` is accepted too so a
    # rename on that side does not read as a database fault.
    answered = served.get("hitl_request_id", served.get("id"))
    if str(answered) != str(row["id"]):
        return "fail", (f"asked for approval {row['id']}, hive-tools "
                        f"answered about {answered!r}")
    if served.get("status") != row["status"]:
        return "fail", (f"approval {row['id']} status: hive-tools says "
                        f"{served.get('status')!r}, the table says "
                        f"{row['status']!r}")
    if served.get("caller_mind") != marker:
        return "fail", (f"approval {row['id']} came back from "
                        f"{served.get('caller_mind')!r} while the table held "
                        f"{marker!r} — hive-tools is still on SQLite")
    return "ok", (f"approval {row['id']} ({row['tool_name']}, "
                  f"{row['status']}); a marker written only into "
                  "tools.hitl_requests came back through hive-tools")


async def _proxy(conn: asyncpg.Connection) -> tuple[str, str]:
    """The proxy's model listing, with a model named only in Postgres."""
    base = os.environ.get("INFERENCE_PROXY_URL",
                          "http://127.0.0.1:8899").rstrip("/")
    token = os.environ.get("INFERENCE_PROXY_KEY") or \
        os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not token:
        raise Absent("no INFERENCE_PROXY_KEY or ANTHROPIC_AUTH_TOKEN in the "
                     "environment; the listing routes need an issued key")
    _reachable(f"{base}/health")
    row = await conn.fetchrow(
        "SELECT id, deployment_name FROM proxy.models WHERE enabled "
        "ORDER BY id LIMIT 1")
    if row is None:
        return "fail", "proxy.models holds no enabled model"
    marker = f"postflight-{uuid.uuid4().hex[:8]}"
    async with _Probe(conn, "proxy.models", "deployment_name", "id",
                      row["id"], marker):
        answer = _call("GET", f"{base}/v1/models", token)
    served = _items(answer, "data")
    if not served:
        return "fail", "the proxy listed no models"
    names = {m.get("id") for m in served if isinstance(m, dict)}
    total = await conn.fetchval("SELECT count(*) FROM proxy.models")
    if marker not in names:
        return "fail", (f"a model renamed to {marker!r} in proxy.models did "
                        "not appear in the proxy's listing — it is still on "
                        "SQLite")
    return "ok", (f"{len(served)} model(s) served against {total} in "
                  "proxy.models; a marker written only into proxy.models "
                  "came back through the proxy")


APPS: tuple[tuple[str, object], ...] = (
    ("hive-comms", _comms),
    ("hive-lucent", _lucent),
    ("hive-site", _console),
    ("hive-tools", _tools),
    ("inference-proxy", _proxy),
)


async def run(dsn: str, *, restore: bool = False) -> Postflight:
    report = Postflight()
    conn = await asyncpg.connect(dsn)
    try:
        if journal_path().exists():
            if restore:
                report.apps.append(AppResult(
                    "probe journal", "absent", await restore_journal(conn)))
                return report
            raise Down(
                f"{journal_path()} is there, so a previous run was killed "
                "inside a probe and a marker is still in a live column. "
                "Re-run with --restore to put the original value back.")
        for name, check in APPS:
            try:
                state, detail = await check(conn)
            except Absent as exc:
                state, detail = "absent", str(exc)
            except Down as exc:
                state, detail = "fail", str(exc)
            except (asyncpg.PostgresError, urllib.error.HTTPError,
                    urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                state, detail = "fail", f"{type(exc).__name__}: {exc}"
            report.apps.append(AppResult(name, state, detail))
    finally:
        await conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("HIVE_DATABASE_URL", ""))
    ap.add_argument("--restore", action="store_true",
                    help="put back what a killed run left marked")
    args = ap.parse_args(argv)
    if not args.dsn:
        print("REFUSED: no database named. Set HIVE_DATABASE_URL or pass "
              "--dsn.", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(run(args.dsn, restore=args.restore))
    except Down as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    width = max(len(a.app) for a in report.apps)
    label = {"ok": "ok    ", "fail": "FAIL  ", "absent": "absent"}
    for a in report.apps:
        print(f"{label[a.state]} {a.app:<{width}}  {a.detail}")
    print(f"\n{len(report.apps)} app(s): {len(report.passing)} on the new "
          f"database, {len(report.failing)} failing, "
          f"{len(report.absent)} unchecked")
    if report.failing:
        print("Failing: " + ", ".join(a.app for a in report.failing))
        return 1
    if report.absent:
        print("Unchecked: " + ", ".join(a.app for a in report.absent)
              + ". Every app that could be checked is on the new database; "
                "these were not checked at all, which is not the same as "
                "passing.")
        return 1
    print("\nEvery app is reading the new database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
