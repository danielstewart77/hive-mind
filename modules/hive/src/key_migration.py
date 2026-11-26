"""Moving existing minds onto one key, accounting for every old-key holder, and
retiring the old keys (feature 834, requirement 16).

A mind made before this feature holds a different value for each service and a
separate inference-proxy key. The proxy key is the person's and is already live,
so it is what becomes the mind's one key: the move registers that same value with
the gateway, the memory store and the tools as that mind, and writes it where the
mind will read it. The proxy is read (`/v1/models`) and never changed.

What a move does and does not keep, plainly. Each service holds one value per
holder, so registering the proxy key replaces the mind's old value there: the old
per-service value stops being accepted at the move, not at retirement. What the
move keeps is the old value *in its file* (the `COMMS_TOKEN_<NAME>` and
`LUCENT_TOKEN_<NAME>` lines, untouched), which is what `undo_move` registers
again. Retirement deletes those lines, so undo is impossible afterwards, and
revokes the shared tools token. A running mind that has not been recreated still
presents its old value and is refused until it is.

Everything here runs inside the console, under the same `_ROTATION_LOCK` as
every other credential change, and goes through the services' own credential
routes. Nothing here prints, logs or returns a key; the report names places,
variables and the first eight hex digits of a digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from pathlib import Path
from typing import Any

import credentials_data
import holder_credentials as hc
import key_journal
from credentials_data import Unreadable
from holder_credentials import MintRefused, ReplacementFailed

#: What the tools' own routes call the holder of the shared service token.
SHARED_HOLDER = "skippy"

REGISTERED_NOT_STORED = (
    "registered, key not stored here: a mind on another machine whose pasted key "
    "is registered at the gateway, the memory store and the tools; the console "
    "never holds that key, and its settings are changed with its owner's key")

REPOINT_NOTE = (
    "The report checks that each tools consumer other than a mind has a credential of its "
    "own; it cannot see which one the consumer presents. Point each bot's and the console's "
    "tools token at the credential issued for it before retiring, or it is refused afterwards.")

RETIREMENT_WARNING = (
    "Retiring ends going back a release for every moved mind: the previous "
    "release's minds present per-service values that are gone, and the move "
    "can no longer be undone for any mind. Do it only after the release you "
    "are on is the one you are keeping.")


def digest_prefix(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _services() -> list[hc.Service]:
    """The three services a mind is accounted on (the tools only where this hive has them)."""
    return [s for s in hc.SERVICES.values()
            if s.key != "proxy" and (s.key != "tools" or s.configured())]


def _tools_applies(holder: str) -> bool:
    """The mind whose name is the shared token's holder takes its tools identity
    when that token is retired, not at its move."""
    return holder != SHARED_HOLDER


def _kind_of(holder: str, role: str, minds: set[str]) -> str:
    name = holder.lower()
    if name in minds:
        return "mind"
    if name == SHARED_HOLDER:
        return "shared tools token"
    if role in ("person", hc.USER_ROLE):
        return "person"
    if name.endswith("-bot"):
        return "surface bot"
    if role == "mind":
        return "unclear"   # typed a mind, but the registry has no such mind
    return "service"


def _is_local(name: str) -> bool:
    """A mind whose folder or env file is on this machine."""
    try:
        import configuration_data
        if (Path(configuration_data.HIVE_MIND_MINDS_CONFIG_DIR) / name / "runtime.yaml").is_file():
            return True
    except Exception:  # noqa: BLE001
        pass
    return hc.own_file_label(name) is not None


async def _registry() -> dict[str, dict[str, Any]]:
    from hive_data import core
    return {str(r["name"]).strip().lower(): dict(r) for r in await core.list_minds()}


def _old_location(service: hc.Service, name: str) -> hc.Location | None:
    try:
        return hc.locate(service, name, old_only=True)
    except (hc.Ambiguous, Unreadable):
        return None


def _stored(name: str) -> hc.Location | None:
    try:
        return hc.stored_key_location(name)
    except MintRefused:
        return None


def _analyse_keys() -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], str | None]:
    """Per mind: the proxy keys found for it (place, digest) and the value if
    they agree; and which minds share a value. Values stay inside this call's
    result and are never serialised: callers strip them with `_public`."""
    try:
        places = hc.key_places()
    except Unreadable as exc:
        return {}, {}, f"{exc} could not be read"
    by_mind: dict[str, dict[str, Any]] = {}
    for place, variable, value in places:
        head = place.partition(":")[0]
        holder = hc.holder_from_place(place)
        if holder is None:
            continue
        mind = by_mind.setdefault(holder, {"found": [], "values": set()})
        mind["found"].append({"place": place, "variable": variable, "digest": digest_prefix(value)})
        mind["values"].add(value)
    shared: dict[str, list[str]] = {}
    owners: dict[str, set[str]] = {}
    for holder, mind in by_mind.items():
        for value in mind["values"]:
            owners.setdefault(value, set()).add(holder)
    for value, names in owners.items():
        if len(names) > 1:
            for name in names:
                shared.setdefault(name, []).extend(sorted(n for n in names if n != name))
    return by_mind, shared, None


async def _proxy_value_of(name: str) -> str | None:
    sources = hc.mind_proxy_key_sources(name)
    values = {v for _, v in sources}
    return next(iter(values)) if len(values) == 1 else None


async def account() -> dict[str, Any]:
    """Every holder of an old key on the gateway, the memory store and the
    tools, and every mind, each with a state. Never a value.

    `ready_to_retire` is true only when no mind is not moved, every service
    could be asked, no minds share a key, and each tools consumer other than a
    mind has a credential of its own; `blocking` says in words what stops it.
    """
    registry = await _registry()
    minds = set(registry)
    services = _services()
    listings: dict[str, list[dict[str, Any]] | None] = {}
    errors: dict[str, str] = {}
    for service in services:
        try:
            listings[service.key] = await hc.list_holders(service)
        except Exception as exc:  # noqa: BLE001 - named, never hidden
            listings[service.key] = None
            errors[service.key] = hc._failure_sentence(exc)

    by_service: list[dict[str, Any]] = []
    names_by_service: dict[str, set[str]] = {}
    row_state: dict[tuple[str, str], str] = {}
    for service in services:
        rows = listings[service.key]
        entry: dict[str, Any] = {"service": service.key, "label": service.label, "holders": []}
        if rows is None:
            entry["error"] = errors[service.key]
        else:
            names_by_service[service.key] = set()
            for row in rows:
                holder = str(row.get("holder") or "").strip()
                if not holder or holder == "shared-token-retired":
                    continue  # the tools' own record that the shared token was retired
                low = holder.lower()
                if str(row.get("role") or "") == hc.USER_ROLE:
                    # A person's key is held at the gateway alone, by design.
                    entry["holders"].append({"holder": holder, "kind": "person", "live": not (
                        row.get("revoked_at") or row.get("live") is False)})
                    continue
                names_by_service[service.key].add(low)
                live = not (row.get("revoked_at") or row.get("live") is False)
                row_state[(service.key, low)] = "live" if live else "revoked"
                entry["holders"].append({
                    "holder": holder,
                    "kind": _kind_of(low, str(row.get("role") or ""), minds),
                    "live": live,
                })
        by_service.append(entry)

    by_mind, shared, unreadable = _analyse_keys()
    blocking: list[str] = []
    if unreadable:
        blocking.append(unreadable)
    for key, message in errors.items():
        blocking.append(f"{hc.SERVICES[key].label} could not be asked: {message}")

    # Minds are the registry's, plus any holder typed a mind that the registry
    # does not know: the role alone is not trusted either way.
    names = set(minds)
    for entry in by_service:
        for holder in entry["holders"]:
            if holder["kind"] == "unclear":
                names.add(holder["holder"].lower())

    mind_rows: list[dict[str, Any]] = []
    for name in sorted(names):
        problems: list[str] = []
        note: str | None = None
        found = by_mind.get(name, {}).get("found", [])
        stored = _stored(name)
        try:
            sources = hc.mind_proxy_key_sources(name) if stored is None else []
        except Unreadable as exc:
            sources = []
            problems.append(f"a file that may hold its key could not be read: {exc}")
        distinct = {v for _, v in sources}
        local = _is_local(name)
        present = {s.key for s in services if row_state.get((s.key, name))}
        if name not in minds:
            state = "unclear"
            problems.append("a holder typed a mind that the mind registry has no row for")
        elif stored is not None or (not local and key_journal.registered_not_stored(name)):
            # Its one key is kept here, or it is a mind on another machine whose
            # key was pasted on its page: registered at the three services, the
            # console never storing it (requirement 19).
            state = "moved"
            here = stored is not None
            if not here:
                note = REGISTERED_NOT_STORED
            for service in services:
                if service.key == "tools" and not _tools_applies(name):
                    continue
                seen = row_state.get((service.key, name))
                if seen == "revoked":
                    problems.append(f"cut off at {service.label} (kept cut off by the move)")
                elif seen != "live":
                    state = "not moved"
                    problems.append(
                        f"holds its one key here but {service.label} has no credential for it"
                        if here else
                        f"was registered by its pasted key but {service.label} has no live credential for it")
        elif not sources:
            state = "not moved"
            problems.append(
                "no proxy key found for this mind in any file this console reads"
                if local else
                "another machine: its key cannot be read or written from here, "
                "so it cannot be moved from here; paste its key on its page to register it")
        elif len(distinct) > 1:
            state = "not moved"
            problems.append("its files hold different proxy keys: " + ", ".join(p for p, _ in sources))
        else:
            state = "not moved"
        for other in sorted(set(shared.get(name, []))):
            state = "not moved"
            problems.append(f"shares its proxy key with {other}")
        for service in services:
            if (service.key in names_by_service and name not in names_by_service[service.key]
                    and any(name in n for n in names_by_service.values())):
                problems.append(f"has no holder row on {service.label} though it has one elsewhere")
        if state != "moved" and name in minds:
            blocking.append(f"{name} is not moved" + (f": {problems[0]}" if problems else ""))
        elif state == "unclear":
            blocking.append(f"{name} is typed a mind but the mind registry does not know it")
        edge = hc.own_file_label(name)
        mind_rows.append({
            "mind": name, "state": state,
            "where_it_reads": (
                f"{stored.file}:{stored.env_key}" if stored is not None else None),
            "note": note,
            "proxy_key_found": [
                {"place": f["place"], "variable": f["variable"], "digest": f["digest"]}
                for f in found],
            "services": {s.key: row_state.get((s.key, name), "none") for s in services},
            "edge_mind_note": (
                "an edge mind: its Telegram and Discord surfaces run on this same key, "
                "so this one holder is both a mind and its surface bots (counted once)"
                if edge else None),
            "shared_tools_note": (
                "holds the shared tools token's name: its tools identity is registered "
                "when that token is retired" if name == SHARED_HOLDER else None),
            "problems": problems,
        })

    # Holder names on one service and not the others, for anything that is
    # not a mind (minds are covered above, per mind).
    seen_somewhere: dict[str, set[str]] = {}
    for key, held in names_by_service.items():
        for low in held:
            seen_somewhere.setdefault(low, set()).add(key)
    partial = sorted(
        {"holder": low, "only_on": sorted(keys)}["holder"] + " (only on " + ", ".join(sorted(keys)) + ")"
        for low, keys in seen_somewhere.items() if len(keys) < len(names_by_service))

    # The shared tools token: consumers other than minds need credentials of their own.
    tools_missing: list[str] = []
    if "tools" in names_by_service:
        consumers = _tools_consumers(by_service)
        tools_missing = [c for c in consumers if row_state.get(("tools", c)) != "live"]
        for c in tools_missing:
            blocking.append(f"{c} has no tools credential of its own (the shared tools token is still its only one)")

    ready = not blocking
    return {
        "services": by_service,
        "minds": mind_rows,
        "names_on_some_services_only": partial,
        "tools_consumers_without_own_credential": tools_missing,
        "blocking": blocking,
        "ready_to_retire": ready,
        "retirement_warning": RETIREMENT_WARNING + " " + REPOINT_NOTE,
    }


def _tools_consumers(by_service: list[dict[str, Any]]) -> list[str]:
    """Non-mind consumers of the shared tools token: this console and every
    surface bot. Named by their gateway holder (a surface bot is a `service`
    row there, known by name) plus the console's own holder."""
    import os
    names: set[str] = set()
    for entry in by_service:
        for holder in entry["holders"]:
            if holder["kind"] == "surface bot":
                names.add(holder["holder"].lower())
    extra = os.environ.get("HIVE_TOOLS_CONSUMERS") or "hive-site"
    for item in extra.split(","):
        if item.strip():
            names.add(item.strip().lower())
    return sorted(names)


# ---------------------------------------------------------------------------
# Compose line for a container mind
# ---------------------------------------------------------------------------

def _compose_path(name: str) -> Path | None:
    try:
        import configuration_data
        path = Path(configuration_data.HIVE_MIND_MINDS_CONFIG_DIR) / name / "container" / "compose.yaml"
    except Exception:  # noqa: BLE001
        return None
    return path if path.is_file() else None


_ENV_HEAD = re.compile(r"(?m)^(?P<indent>[ \t]+)environment:[ \t]*\n(?P<first>[ \t]+)- ")


def _compose_line(name: str) -> str:
    return f"MIND_KEY=${{{hc.one_key_variable(name)}}}"


def compose_one_key(name: str, *, add: bool) -> str:
    """Add (or remove) the one line that makes a container mind read its key
    as MIND_KEY. Text-level, so nothing else in the file is touched; a file the
    console cannot find, write, or whose layout is not one `environment:` list
    is left alone and the answer says so."""
    path = _compose_path(name)
    if path is None:
        return "not found: add the line MIND_KEY=${%s} to the mind's compose environment by hand" % hc.one_key_variable(name)
    try:
        text = credentials_data._read_text(path)
    except Unreadable:
        return "not readable"
    line = _compose_line(name)
    present = re.search(r"(?m)^[ \t]+-[ \t]+['\"]?" + re.escape(line) + r"['\"]?[ \t]*$", text) is not None
    if add:
        if present or re.search(r"(?m)^[ \t]+-[ \t]+MIND_KEY=", text):
            return "already present"
        heads = list(_ENV_HEAD.finditer(text))
        if len(heads) != 1:
            return "left alone: the file does not have exactly one environment list; add the line by hand"
        match = heads[0]
        insert = f"{match.group('first')}- {line}\n"
        cut = match.start("first")
        updated = text[:cut] + insert + text[cut:]
    else:
        if not present:
            return "already absent"
        updated = re.sub(r"(?m)^[ \t]+-[ \t]+['\"]?" + re.escape(line) + r"['\"]?[ \t]*\n", "", text, count=1)
    try:
        with credentials_data._WRITE_LOCK:
            credentials_data._write_file_in_place(path, updated, "compose", name, text)
    except OSError as exc:
        return f"not writable ({exc.strerror or exc})"
    return "added" if add else "removed"


# ---------------------------------------------------------------------------
# Move and undo
# ---------------------------------------------------------------------------

def _line_remove(path: Path, variable: str) -> None:
    with credentials_data._WRITE_LOCK:
        text = credentials_data._read_text(path)
        kept = [ln for ln in text.splitlines(keepends=True) if not ln.startswith(f"{variable}=")]
        if len(kept) != len(text.splitlines(keepends=True)):
            credentials_data._write_file_in_place(path, "".join(kept), hc.MIND_ENV_LABEL, variable, text)


async def _validated_value(name: str) -> str:
    """The proxy key this mind already holds, checked: one value across all the
    places it is kept, live at the proxy, and not another mind's."""
    try:
        sources = hc.mind_proxy_key_sources(name)
    except Unreadable as exc:
        raise MintRefused(f"{exc} could not be read, so {name}'s key cannot be found. Nothing was changed") from None
    if not sources:
        raise MintRefused(
            f"{name} has no proxy key in any file this console reads"
            if _is_local(name) else
            f"{name} is on another machine: its key cannot be read or written from "
            "here, so it cannot be moved from here; paste its key on its page to register it")
    values = {v for _, v in sources}
    if len(values) > 1:
        raise MintRefused(
            f"{name}'s files hold different proxy keys ({', '.join(p for p, _ in sources)}); "
            "nothing was changed")
    value = next(iter(values))
    import provider_data
    connection = await provider_data.connection_for_mind(name)
    here = hc.own_file_label(name) is None
    if connection and connection["id"] != "hive-proxy":
        checked, _ = await hc.accept_mind_provider_token(value, for_mind=name, stored_here=here)
    else:
        checked, _ = await hc.validate_mind_proxy_key(value, for_mind=name, stored_here=here)
    return checked


async def move_mind(name: str) -> dict[str, Any]:
    """Register the proxy key a mind already holds as its one key.

    Raises `MintRefused` or `KeyHeldByAnother` when nothing changed, and
    `ReplacementFailed` when something did and could not be fully put back.
    """
    holder = name.strip().lower()
    registry = await _registry()
    row = registry.get(holder)
    if row is None:
        raise MintRefused(f"{name} is not a mind this hive knows")
    if _stored(holder) is not None:
        raise MintRefused(f"{holder} already holds one key; nothing to move")
    path = hc.ENV_FILES.get(hc.MIND_ENV_LABEL)
    own_label = hc.own_file_label(holder)
    if own_label:
        target = hc.Location(own_label, hc.ONE_KEY_PLAIN)
    elif path is not None and path.is_file():
        target = hc.Location(hc.MIND_ENV_LABEL, hc.one_key_variable(holder))
    else:
        raise MintRefused("This Hive is not configured to store mind keys here; nothing was changed")
    services = [s for s in _services() if s.key != "tools" or _tools_applies(holder)]
    owner = str(row.get("owner") or "")
    mind_id = str(row.get("mind_id") or "")

    async with hc._ROTATION_LOCK:
        value = await _validated_value(holder)
        old: dict[str, str | None] = {}
        rows: dict[str, dict[str, Any] | None] = {}
        for service in services:
            try:
                rows[service.key] = await hc._holder_row(service, holder)
            except MintRefused as exc:
                raise MintRefused(f"{exc}. Nothing was changed.") from None
            location = _old_location(service, holder)
            if service.key == "tools":
                if rows[service.key] is not None:
                    raise MintRefused(
                        "the tools already hold a credential for this mind that this console "
                        "cannot read; nothing was changed")
                old[service.key] = None
                continue
            if rows[service.key] is not None and location is None:
                raise MintRefused(
                    f"{service.label} holds a value for {holder} that is in no file this "
                    "console can read, so a failed move could not be put back. Nothing was changed")
            old[service.key] = hc._loaded_value(location) if location else None
        for service in services:
            answer = await hc._accepts(service, value)
            if answer is None:
                raise MintRefused(f"Could not check that key against {service.label}. Nothing was changed.")
        try:
            record = key_journal.begin("move", holder, [s.key for s in services])
            record["owner"], record["mind_id"] = owner, mind_id
            record["revoked"] = [k for k, r in rows.items() if hc._is_revoked(r)]
            key_journal.update(record)
        except OSError as exc:
            raise MintRefused(
                f"the journal of key changes could not be written ({exc.strerror or exc}). "
                "Nothing was changed.") from None

        state: dict[str, str] = {}
        failure: Exception | None = None
        cancelled = False
        wrote_line = False
        try:
            if target.file == hc.MIND_ENV_LABEL:
                hc._ensure_key(target.path, target.env_key)
            with credentials_data._WRITE_LOCK:
                written = credentials_data._write_location(
                    target.path, target.env_key, value, target.file)
                try:
                    verified = bool(written["written"]) and hc._loaded_value(target) == value
                except Unreadable:
                    verified = False
            wrote_line = True
            if not verified:
                raise MintRefused(
                    f"{target.path} did not take the key"
                    + (f" ({written['reason']})" if written.get("reason") else ""))
        except Exception as exc:  # noqa: BLE001
            failure = exc
        if failure is None:
            for service in services:
                try:
                    await hc._put_key(service, holder, owner, mind_id, rows[service.key], value)
                except hc.MintUncertain as exc:
                    state[service.key] = "unknown"
                    failure = exc
                    break
                except asyncio.CancelledError:
                    state[service.key] = "unknown"
                    failure = MintRefused("the request was cancelled")
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    state[service.key] = "untouched"
                    failure = exc
                    break
                state[service.key] = "new"
                try:
                    key_journal.update(record, switched=[k for k, v in state.items() if v == "new"])
                except OSError as exc:
                    failure = MintRefused(f"the journal could not be written ({exc.strerror or exc})")
                    break
        compose = "not attempted"
        if failure is None:
            for service in services:
                if hc._is_revoked(rows[service.key]) and service.key != "tools":
                    # A put clears a revocation; a mind cut off before the move stays cut off.
                    try:
                        await hc._revoke_row(service, holder)
                    except Exception as exc:  # noqa: BLE001
                        failure = ReplacementFailed(
                            f"{service.label} could not be cut off again after the move ({hc._failure_sentence(exc)})")
                        break
        if failure is None:
            compose = compose_one_key(holder, add=True) if not own_label else "not needed: an edge mind reads its own file"
            try:
                key_journal.finish(record)
                key_journal.clear(holder)
            except OSError:
                pass
            return {
                "mind": holder,
                "services": [{"service": s.key, "label": s.label, "status": "moved"} for s in services]
                            + ([] if _tools_applies(holder) or not hc.SERVICES["tools"].configured() else
                               [{"service": "tools", "label": hc.SERVICES["tools"].label,
                                 "status": "registered when the shared tools token is retired"}]),
                "stored": f"{target.file}:{target.env_key}",
                "compose": compose,
                "restart_target": target.restart_target,
                "next_step": (
                    "Recreate this mind now so it reads its one key; until then its old values are "
                    "refused. The old per-service values stay in their file until retirement, and "
                    "'undo' registers them again."),
            }

        # Put back, in reverse order, whatever may have taken the new key.
        async def put_back() -> bool:
            for service in reversed(services):
                if state.get(service.key) not in ("new", "unknown"):
                    continue
                try:
                    previous = old.get(service.key)
                    if previous is None:
                        response = await hc._request(service, "DELETE", f"/credentials/{holder}?remove=true")
                        if response.status_code >= 400:
                            raise MintRefused("not removed")
                        state[service.key] = "none"
                    else:
                        await hc._put_key(service, holder, owner, mind_id, rows[service.key], previous)
                        if hc._is_revoked(rows[service.key]):
                            await hc._revoke_row(service, holder)
                        state[service.key] = "old"
                except Exception:  # noqa: BLE001 - reported below
                    pass
            ok = True
            if wrote_line:
                try:
                    _line_remove(target.path, target.env_key)
                    ok = hc._loaded_value(target) is None
                except Exception:  # noqa: BLE001
                    ok = False
            return ok

        task = asyncio.ensure_future(put_back())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    pass
        line_ok = task.result() if not task.cancelled() and task.exception() is None else False
        clean = line_ok and all(state.get(s.key, "untouched") in ("old", "none", "untouched") for s in services)
        reason = hc._failure_sentence(failure)
        if cancelled:
            try:
                if clean:
                    key_journal.finish(record)
                else:
                    key_journal.update(record, status="needs_attention",
                                       detail=hc._states_sentence(services, state))
            except OSError:
                pass
            raise asyncio.CancelledError()
        if clean:
            try:
                key_journal.finish(record)
            except OSError:
                pass
            held = hc._held_by_name(failure)
            if held or "already held by" in str(failure):
                raise hc.KeyHeldByAnother(hc.key_held_message(held))
            raise MintRefused(f"{reason}. Every service is back on its old value; nothing moved.")
        sentence = hc._states_sentence(services, state)
        if not line_ok:
            sentence += f"; the line {target.env_key} in {target.path} could not be taken out again"
        try:
            key_journal.update(record, status="needs_attention", detail=sentence)
        except OSError:
            pass
        raise ReplacementFailed(
            f"{reason}. Now: {sentence}. Check each service, then press move again; it is safe to repeat.")


def _retired_marker() -> Path:
    return key_journal.JOURNAL_DIR / "retired.marker"


async def undo_move(name: str) -> dict[str, Any]:
    """Put a moved mind back on its old per-service values and remove its
    one-key registration. For that mind alone, and only until retirement: the
    old values are read from their own file, and are gone once retired."""
    holder = name.strip().lower()
    if _retired_marker().exists():
        raise MintRefused(
            "the old keys were retired, so no mind can be put back on its old values")
    registry = await _registry()
    row = registry.get(holder)
    if row is None:
        raise MintRefused(f"{name} is not a mind this hive knows")
    stored = _stored(holder)
    if stored is None:
        raise MintRefused(f"{holder} is not on one key; nothing to undo")
    services = [s for s in _services() if s.key != "tools" or _tools_applies(holder)]
    owner, mind_id = str(row.get("owner") or ""), str(row.get("mind_id") or "")
    async with hc._ROTATION_LOCK:
        if _retired_marker().exists():  # a retirement may have run while this waited
            raise MintRefused(
                "the old keys were retired, so no mind can be put back on its old values")
        old: dict[str, str] = {}
        for service in services:
            if service.key == "tools":
                continue
            location = _old_location(service, holder)
            value = hc._loaded_value(location) if location else None
            if not value:
                raise MintRefused(
                    f"{holder}'s old {service.label} value is not in any file this console reads "
                    "(it was never moved by this tool, or the old keys were retired), so it "
                    "cannot be put back. Nothing was changed.")
            old[service.key] = value
        rows: dict[str, dict[str, Any] | None] = {}
        for service in services:
            rows[service.key] = await hc._holder_row(service, holder)
        done: list[str] = []
        failures: list[str] = []
        for service in services:
            try:
                if service.key == "tools":
                    response = await hc._request(service, "DELETE", f"/credentials/{holder}?remove=true")
                    if response.status_code >= 400:
                        raise MintRefused(f"{service.label} did not remove its credential")
                else:
                    await hc._put_key(service, holder, str(row.get("owner") or owner), mind_id,
                                      rows[service.key], old[service.key])
                    if hc._is_revoked(rows[service.key]):
                        await hc._revoke_row(service, holder)
                done.append(service.key)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{service.label}: {hc._failure_sentence(exc)}")
        if failures:
            raise ReplacementFailed(
                "; ".join(failures) + ". The services that answered are back on their old values "
                "and the one-key registration is still in place; press undo again to finish.")
        _line_remove(stored.path, stored.env_key)
        compose = compose_one_key(holder, add=False) if not hc.own_file_label(holder) else "not needed"
        key_journal.clear(holder)
    return {
        "mind": holder,
        "services": [{"service": s.key, "status": "back on its old value" if s.key != "tools"
                      else "one-key registration removed"} for s in services],
        "compose": compose,
        "next_step": "Recreate this mind so it reads its old values again.",
    }


# ---------------------------------------------------------------------------
# Tools credentials for the consumers that are not minds
# ---------------------------------------------------------------------------

async def issue_tools_credential(holder: str) -> dict[str, Any]:
    """Give a surface bot or the console a tools credential of its own, so the
    shared token can be retired without them. A random value is registered with
    the tools under the holder's name and written once to `TOOLS_TOKEN_<HOLDER>`
    in the shared env file, where the consumer's compose is pointed at it (that
    repoint is the operator's). Never returned."""
    name = holder.strip().lower()
    tools = hc.SERVICES["tools"]
    if not tools.configured():
        raise MintRefused("this hive has no tools to issue a credential on")
    if name in await _registry():
        raise MintRefused(f"{name} is a mind: it is moved, not issued a separate credential")
    if name in hc.TOOLS_RESERVED_HOLDERS:
        raise MintRefused(f"{name!r} is a name the tools keep for themselves")
    path = hc.ENV_FILES.get(hc.MIND_ENV_LABEL)
    if path is None or not path.is_file():
        raise MintRefused("no shared env file to write the credential into; nothing was changed")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise MintRefused("a holder here is lowercase letters, digits and hyphens")
    variable = "TOOLS_TOKEN_" + name.replace("-", "_").upper()
    async with hc._ROTATION_LOCK:
        if await hc._holder_row(tools, name) is not None:
            raise MintRefused(
                f"the tools already hold a credential for {name}; it is not replaced by this tool. "
                "Nothing was changed")
        if credentials_data._read_env_value(path, variable) is not None:
            raise MintRefused(f"{variable} is already in {path}; nothing was changed")
        value = secrets.token_urlsafe(32)
        try:
            await hc._put(tools, name, "", value, "service", None)
        except hc.MintUncertain:
            try:
                await hc._request(tools, "DELETE", f"/credentials/{name}?remove=true")
            except Exception:  # noqa: BLE001
                pass
            raise MintRefused("the tools did not answer, so whether a credential was registered is unknown; "
                             "a removal was attempted. Check the tools' holder list before trying again") from None
        try:
            hc._ensure_key(path, variable)
            with credentials_data._WRITE_LOCK:
                written = credentials_data._write_location(path, variable, value, hc.MIND_ENV_LABEL)
            if not written["written"] or hc._loaded_value(hc.Location(hc.MIND_ENV_LABEL, variable)) != value:
                raise MintRefused(f"{path} did not take the credential")
        except Exception as exc:  # noqa: BLE001 - the tools row would hold a value nobody has
            try:
                _line_remove(path, variable)
            except Exception:  # noqa: BLE001
                pass
            try:
                await hc._request(tools, "DELETE", f"/credentials/{name}?remove=true")
            except Exception:  # noqa: BLE001
                pass
            raise MintRefused(f"{hc._failure_sentence(exc)}. The tools credential was taken out again.") from None
    return {"holder": name, "variable": variable, "file": hc.MIND_ENV_LABEL,
            "next_step": f"Point the consumer's compose at ${{{variable}}} for its tools token and recreate it."}


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

def _compose_texts() -> str | None:
    """The text of every compose file this console can read that names a
    mind's variables: the stack file and each mind's own. Read only to see
    which variables are still referenced; never printed."""
    texts: list[str] = []
    try:
        import configuration_data
        stack = Path(configuration_data.stack_compose_path())
        files = [stack]
        files += sorted(Path(configuration_data.HIVE_MIND_MINDS_CONFIG_DIR).glob("*/container/compose.yaml"))
        texts.append(stack.read_text())  # the stack file unreadable: None, and nothing is deleted
    except Exception:  # noqa: BLE001
        return None
    for path in files[1:]:
        try:
            texts.append(Path(path).read_text())
        except Exception:  # noqa: BLE001
            return None
    return "\n".join(texts)


class RetirementRefused(Exception):
    """Retirement did not run: lists what is not accounted for."""

    def __init__(self, blocking: list[str]):
        super().__init__("; ".join(blocking))
        self.blocking = blocking


async def retire() -> dict[str, Any]:
    """Retire the old per-service mind keys and the shared tools token.

    Refuses, naming each, while any mind is not moved, a service cannot be
    asked, or a tools consumer has no credential of its own. When it runs:
    the old per-service lines of every moved mind leave the shared env file
    (undo is then impossible), the shared tools token is revoked at the tools,
    and the mind that held its name registers its own key there. Service,
    person and surface-bot holders and every administrator key are not
    touched. Repeating it is safe. Afterwards each moved mind's key is read
    back from every service, and each old value that can be read is probed:
    the result says which still answer."""
    tools = hc.SERVICES["tools"]
    registry = await _registry()
    old_values: list[tuple[hc.Service, str, str]] = []
    removed: list[str] = []
    kept: list[str] = []
    async with hc._ROTATION_LOCK:
        # Judged under the lock every move and undo takes, so a mind undone
        # between the check and the revocation cannot be retired out from under.
        report = await account()
        if not report["ready_to_retire"]:
            raise RetirementRefused(report["blocking"])
        for name in sorted(registry):
            for service in (hc.SERVICES["comms"], hc.SERVICES["lucent"]):
                location = _old_location(service, name)
                if location is not None:
                    value = hc._loaded_value(location)
                    if value:
                        old_values.append((service, name, value))
        shared_value = None
        # Before the first irreversible step, whether or not the tools are
        # configured: once the marker exists no undo can bring an old value back,
        # whatever fails after this. A marker that already existed (a repeat) is
        # never taken away.
        existed = _retired_marker().exists()
        # Defence in depth: the readiness report above already refuses when the tools
        # cannot be asked, so this is not reachable today and has no test.
        if tools.configured() and not tools.admin_token():
            raise MintRefused(
                f"this console holds no {tools.admin_env}, so it cannot retire the shared "
                "tools token; nothing was retired")
        try:
            key_journal.JOURNAL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            _retired_marker().write_text("retired\n")
        except OSError as exc:
            if not existed:
                _retired_marker().unlink(missing_ok=True)
            raise MintRefused(
                f"the record that retirement happened could not be written "
                f"({exc.strerror or exc}); nothing was retired") from None
        if tools.configured():
            import os
            import httpx
            shared_value = os.environ.get("HIVE_TOOLS_TOKEN") or None
            try:
                retired = await hc._request(tools, "POST", "/credentials/shared-token/retire")
            except Exception as exc:
                # Only a request that was never sent proves nothing was retired.
                # A timeout may have landed, so the marker stays and undo stays closed.
                if not existed and isinstance(exc.__cause__, httpx.ConnectError):
                    _retired_marker().unlink(missing_ok=True)
                raise
            if retired.status_code != 200:
                if not existed and 400 <= retired.status_code < 500 and retired.status_code != 408:
                    _retired_marker().unlink(missing_ok=True)  # a clean refusal: nothing retired
                raise MintRefused(
                    f"the tools did not retire the shared token (they answered {retired.status_code}); "
                    "the old mind keys were left as they were"
                    + ("" if 400 <= retired.status_code < 500 and retired.status_code != 408 else
                       ", but the tools may have retired it before failing, so undo stays closed; "
                       "check the tools and run retire again"))
            holder = SHARED_HOLDER
            if holder in registry:
                location = _stored(holder)
                value = hc._loaded_value(location) if location else None
                if not value:
                    raise MintRefused(
                        "the shared tools token was retired but the stored key of the mind that held "
                        "its name could not be read; register it by hand")
                row = await hc._holder_row(tools, holder)
                await hc._put_key(tools, holder, str(registry[holder].get("owner") or ""),
                                  str(registry[holder].get("mind_id") or ""), row, value)
        compose_text = _compose_texts()
        for name in sorted(registry):
            for service in (hc.SERVICES["comms"], hc.SERVICES["lucent"]):
                location = _old_location(service, name)
                if location is None or location.file not in (hc.MIND_ENV_LABEL, hc.own_file_label(name)):
                    continue
                if compose_text is None or re.search(
                        r"\$\{?" + re.escape(location.env_key) + r"\b", compose_text):
                    # A bot reads the mind's old gateway value as its proactive
                    # token (`MIND_PROACTIVE_TOKEN=${COMMS_TOKEN_<MIND>}`). The
                    # service has refused the value since the move, so keeping
                    # the line keeps nothing alive; deleting it breaks the bot.
                    kept.append(f"{location.file}:{location.env_key}")
                    continue
                _line_remove(location.path, location.env_key)
                removed.append(f"{location.file}:{location.env_key}")
    still: list[str] = []
    for service, name, value in old_values:
        if await hc._accepts(service, value) is True:
            still.append(f"{name} on {service.label}")
    shared_answer = None
    if shared_value and tools.configured():
        shared_answer = await hc._accepts(tools, shared_value)
    keys_ok: list[str] = []
    for name in sorted(registry):
        location = _stored(name)
        value = hc._loaded_value(location) if location else None
        if not value:
            if location is None and not _is_local(name) and key_journal.registered_not_stored(name):
                keys_ok.append(f"{name}: registered, key not stored here")
            continue
        answers = {s.key: await hc._accepts(s, value) for s in _services()}
        keys_ok.append(f"{name}: " + ", ".join(f"{k} {'accepts' if v else 'REFUSES' if v is False else 'unknown'}"
                                                for k, v in sorted(answers.items())))
    return {
        "retired": True,
        "old_lines_removed": removed,
        "old_lines_kept_because_a_compose_file_reads_them": kept,
        "shared_tools_token": ("refused on the next request" if shared_answer is False else
                               "still accepted" if shared_answer else "not checked here"),
        "old_values_still_accepted": still,
        "moved_minds": keys_ok,
        "warning": RETIREMENT_WARNING + " " + REPOINT_NOTE,
    }
