"""Creating a Mind is all or nothing, and stays so across a restart.

A Mind made on the Add a Mind page is six things written in six places: a
role assignment, a folder, three credentials and their lines in an env file,
a line in the stack file, a node in the graph and a row at the gateway. A
creation that stops part way used to leave whichever of them it had reached,
reported one by one beside a mind that could not run, and the name was then
taken by something nobody could finish or remove from the console.

So each step is recorded as it is taken, in a small file of this console's
own, and a creation that fails takes back exactly what it recorded, newest
first. The record outlives the request: a console that is restarted in the
middle of a creation — which is what running the installer again does — finds
it at its next start and takes the half-made Mind back then. A step is struck
from the record as it is taken back, so a later pass tries only what is left.

A record found at start is older than the process reading it, and the name in
it may since have become another Mind's. Three of the six steps are known by
name alone, so a record is only acted on then once the gateway and the folder
both say the name is still nobody else's.

Starting the Mind's container is deliberately not one of the steps. It is not
this console's to take back: the container is the host's, building its image
can outlast the wait Hive Tools allows and finish after it was reported
failed, and deleting a Mind's folder under a container that then comes up is
worse than a Mind that is made and not yet running. The record is closed
before the start is asked for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from fastapi import HTTPException

import configuration_data
import holder_credentials
import mind_soul
import provider_data
import store

#: Where a creation in progress is recorded. The console's own data folder,
#: which outlives its container.
CREATION_DIR = Path(os.environ.get("MIND_CREATION_DIR", "/data/mind-creations"))

#: What each recorded step is called when it could not be taken back.
STEP_LABELS = {
    "provider": "its provider connection",
    "roles": "its role assignment",
    "folder": "its folder",
    "credentials": "its credentials",
    "stack": "its line in the stack file",
    "node": "its node in the graph",
    "gateway": "its registration with the gateway",
}


log = logging.getLogger(__name__)

#: The steps known only by the Mind's name. The folder carries the Mind's
#: id, the gateway row and the role assignment are keyed by it.
NAME_KEYED = ("credentials", "stack", "node", "provider")


class InProgress(Exception):
    """A Mind of this name is being made, or was and has not been cleared."""


class Creation:
    """One Mind being made: who it is, and which steps have been taken."""

    def __init__(self, name: str, mind_id: str, owner: str,
                 done: list[str] | None = None) -> None:
        self.name = name
        self.mind_id = mind_id
        self.owner = owner
        self.done: list[str] = list(done or [])

    @property
    def path(self) -> Path:
        return CREATION_DIR / f"{self.name}.json"

    def _text(self) -> str:
        return json.dumps({"name": self.name, "mind_id": self.mind_id,
                           "owner": self.owner, "done": self.done}) + "\n"

    def begin(self) -> None:
        """Open the record, before anything is written for this Mind.

        Exclusive, so it is also what stops two creations of one name — a
        button pressed twice — from undoing each other's work.
        """
        CREATION_DIR.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise InProgress(
                f"A Mind called {self.name} is already being made, or an earlier "
                "attempt at it has not been cleared yet") from exc
        try:
            with os.fdopen(handle, "w") as stream:
                stream.write(self._text())
        except OSError:
            # A record that could not be written records nothing, and left
            # there it would hold the name for good.
            self.path.unlink(missing_ok=True)
            raise

    def _save(self) -> None:
        scratch = self.path.with_name(self.path.name + ".new")
        with open(scratch, "w") as stream:
            stream.write(self._text())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch, self.path)

    def mark(self, step: str) -> None:
        """Record a step: before it runs when it can fail half done, so that
        what it half did is taken back too; after it, when it cannot."""
        if step not in STEP_LABELS:
            raise ValueError(f"not a step of creating a Mind: {step}")
        if step in self.done:
            return
        self.done.append(step)
        self._save()

    def strike(self, step: str) -> None:
        """A step has been taken back, or was never this creation's."""
        if step in self.done:
            self.done.remove(step)
            self._save()

    def close(self) -> None:
        """The Mind is made, or wholly taken back: nothing left to record."""
        self.path.unlink(missing_ok=True)


def is_open(name: str) -> bool:
    """Whether a creation of this name is recorded and not closed."""
    return (CREATION_DIR / f"{name}.json").exists()


def pending() -> list[Creation]:
    """Every creation recorded and not closed, oldest first.

    A record that does not read as one is removed, and said: it is the
    remains of a write that never finished, it names no step that could be
    taken back, and left there it holds its name for good.
    """
    found: list[tuple[float, Creation]] = []
    try:
        entries = sorted(CREATION_DIR.glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            raw = json.loads(path.read_text())
            creation = Creation(str(raw["name"]), str(raw["mind_id"]),
                                str(raw["owner"]), [str(s) for s in raw.get("done") or []])
            if creation.path != path or any(s not in STEP_LABELS for s in creation.done):
                raise ValueError("not a record of a creation")
            found.append((path.stat().st_mtime, creation))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("removing an unreadable mind-creation record %s: %s", path.name, exc)
            try:
                path.unlink()
            except OSError:
                pass
    return [creation for _, creation in sorted(found, key=lambda item: item[0])]


class _AlreadyGone(Exception):
    """What a step would take back is not there to take back."""


async def _undo_node(creation: Creation) -> None:
    # As the Mind, with its own credential: which is why this runs before
    # the credentials are taken back.
    location = holder_credentials.locate(
        holder_credentials.SERVICES["lucent"], creation.name)
    credential = holder_credentials._loaded_value(location) if location else None
    if not credential:
        # Without the Mind's credential the node cannot be deleted as the
        # Mind, now or on any later pass. Said once, by the caller, and not
        # retried for ever.
        raise _AlreadyGone(
            "its node in the graph, if one was written, could not be deleted: "
            "the credential that wrote it is gone")
    try:
        await mind_soul.remove_mind_node(creation.name, creation.owner, credential)
    except HTTPException as exc:
        if "not_found" in str(exc.detail) or "not found" in str(exc.detail).lower():
            return
        raise


async def _undo_credentials(creation: Creation) -> None:
    failures = await holder_credentials.unprovision_mind(creation.name)
    if failures:
        raise RuntimeError("; ".join(failures))


async def unmake(
    creation: Creation,
    deregister: Callable[[str, str], Awaitable[Any]],
    *,
    skip: tuple[str, ...] = (),
) -> list[str]:
    """Take back what this creation recorded, newest first.

    Every step is tried whatever the ones before it did — a gateway that
    cannot be reached must not leave the folder and the keys behind as well —
    with one exception: the credentials are kept while the node is still
    there, because the node is deleted with them. Each step is struck from
    the record as it is taken back. Returns what could not be, in words for
    the page: a creation that failed and left something is said to have,
    never reported clean. The record is closed only when nothing is left;
    otherwise it stays, and the next start of this console tries again.

    `skip` names steps to strike without acting on them: see `sweep`.
    """
    actions: dict[str, Callable[[], Awaitable[Any]]] = {
        "provider": lambda: provider_data.unbind_mind(creation.name),
        "gateway": lambda: deregister(creation.name, creation.mind_id),
        "node": lambda: _undo_node(creation),
        "stack": lambda: asyncio.to_thread(
            configuration_data.exclude_mind_from_stack, creation.name),
        "credentials": lambda: _undo_credentials(creation),
        "folder": lambda: asyncio.to_thread(
            configuration_data.remove_internal_mind, creation.name, creation.mind_id),
        "roles": lambda: store.set_mind_roles(creation.mind_id, []),
    }
    leftovers: list[str] = []
    for step in reversed(list(creation.done)):
        if step in skip:
            creation.strike(step)
            continue
        if step == "credentials" and "node" in creation.done:
            leftovers.append(
                f"{STEP_LABELS[step]} (kept until its node in the graph is deleted with them)")
            continue
        try:
            await actions[step]()
        except _AlreadyGone as exc:
            # Nothing a later pass could do either: said, on the page as
            # well, and let go.
            log.warning("creating %s: %s", creation.name, exc)
            leftovers.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported, the rest still runs
            reason = str(getattr(exc, "detail", "") or exc) or type(exc).__name__
            leftovers.append(f"{STEP_LABELS[step]} ({reason})")
            continue
        creation.strike(step)
    # Closed on what is still recorded, not on what was said: something that
    # can never be taken back is reported and must not hold the name.
    if not creation.done:
        creation.close()
    return leftovers


def _folder_is_anothers(creation: Creation) -> bool:
    """Whether the folder of this name now belongs to a different Mind."""
    runtime = configuration_data.HIVE_MIND_MINDS_CONFIG_DIR / creation.name / "runtime.yaml"
    try:
        loaded = yaml.safe_load(runtime.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    held = str(loaded.get("mind_id") or "") if isinstance(loaded, dict) else ""
    return bool(held) and held != creation.mind_id


async def sweep(
    deregister: Callable[[str, str], Awaitable[Any]],
    name_owner: Callable[[str], Awaitable[str | None]],
) -> list[dict[str, Any]]:
    """Take back every creation a stopped console left half made.

    Run once, when this console starts: any record still open then belongs
    to a request that no longer exists.

    `name_owner` answers, from the gateway, the id of the Mind now registered
    under a name, "" for none, and raises when the gateway cannot say. A
    record whose name has since become another Mind's — at the gateway, or
    in the minds folder — has the steps known only by name struck, not
    taken back: taking them back would revoke a live Mind's credentials and
    delete its node. A record the gateway cannot be asked about is left for
    the next start.
    """
    results = []
    for creation in pending():
        steps = list(creation.done)
        try:
            registered = await name_owner(creation.name)
        except Exception as exc:  # noqa: BLE001 - reported; tried again next start
            results.append({"mind_name": creation.name, "mind_id": creation.mind_id,
                            "steps": steps, "left_behind": [
                                f"everything: the gateway could not say whose the name is ({exc})"]})
            continue
        anothers = (bool(registered) and registered != creation.mind_id) \
            or await asyncio.to_thread(_folder_is_anothers, creation)
        leftovers = await unmake(creation, deregister,
                                 skip=NAME_KEYED if anothers else ())
        results.append({"mind_name": creation.name, "mind_id": creation.mind_id,
                        "steps": steps, "left_behind": leftovers,
                        "name_now_anothers": anothers})
    return results
