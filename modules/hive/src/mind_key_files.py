"""Where a container mind's one key is kept: a file in a folder of its own.

Feature 834, requirement 21. The key used to be a line in the install's shared
`.env`, copied into the container's creation settings, so replacing it meant
recreating the container. It is now one file per mind, `<root>/<mind>/key`,
under a root OUTSIDE the checkout. Each mind's folder is mounted read-only
into that mind and no other, as `/run/mind-key`, and the mind reads it with a
modification-time cache (`core/mind_key.py`, `refresh`) on every request and
spawn. Replacing the key is then a file write and needs no restart.

Two rules this module keeps and the rest of the console relies on:

* A FOLDER is mounted, never a single file. A single-file bind mount cannot be
  replaced atomically, and a reader of a half-written key would present a
  value no service accepts. `write` writes a temporary file in the same
  folder, fsyncs it, and renames it over the old one, so a reader sees the old
  key or the new one and never a part of either.
* Only the console writes here (the root is mounted read-write into the
  console alone). The file holds the key and nothing else; nothing in this
  module logs, returns in an error, or prints a key.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

#: Where the console sees the key root (read-write). The host path behind it is
#: `HOST_KEY_DIR`, outside the checkout, which the generated compose uses.
KEY_ROOT = Path(os.environ.get("HIVE_KEY_ROOT", "/mind_keys"))
#: The file's name in a mind's folder, and where its folder appears in the mind.
FILE_NAME = "key"
MOUNT = "/run/mind-key"

#: What a key looks like. The same rule `core/mind_key.py` reads by (the mind
#: and the console are separate images, so it is kept equal by hand, and a test
#: holds the two together).
KEY_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{8,4096}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class KeyFileError(Exception):
    """The key file could not be read or written; the message names no key."""


def _checked(name: str) -> str:
    stem = str(name).strip().lower()
    if not _NAME.fullmatch(stem):
        raise KeyFileError(f"{name!r} is not a mind name a key folder can be made for")
    return stem


def configured() -> bool:
    """Whether this console was given a key root to write."""
    return KEY_ROOT.is_dir()


def folder(name: str) -> Path:
    return KEY_ROOT / _checked(name)


def path(name: str) -> Path:
    return folder(name) / FILE_NAME


def names() -> list[str]:
    """Every mind that has a key folder here, by name."""
    try:
        return sorted(p.name for p in KEY_ROOT.iterdir()
                      if p.is_dir() and _NAME.fullmatch(p.name) and (p / FILE_NAME).is_file())
    except OSError:
        return []


def exists(name: str) -> bool:
    try:
        return path(name).is_file()
    except KeyFileError:
        return False


def read(name: str) -> str | None:
    """The key held for this mind, or None when it has no file.

    Raises `KeyFileError` for a file that is there and cannot be read: that is
    not "no key", and the caller must not treat it as one."""
    target = path(name)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyFileError(f"{target} could not be read ({type(exc).__name__})") from None
    return text.strip()


def _owner() -> tuple[int, int] | None:
    raw = os.environ.get("HIVE_MIND_UID", "").strip()
    try:
        uid = int(raw) if raw else 1000
        gid = int(os.environ.get("HIVE_MIND_GID", "").strip() or uid)
    except ValueError:
        return None
    return uid, gid


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _hand_over(target: Path, owner: tuple[int, int]) -> None:
    """Make the mind's uid and gid the owner, or fail.

    Failing closed: a console that may not chown (it is not root and the mind
    runs as someone else) would otherwise have to leave the key readable by
    others on the host (0644) for the mind to read it. A key is never loosened
    to make a write succeed; the person fixes HIVE_MIND_UID or the console's
    user, and the old key stays in place meanwhile."""
    info = os.stat(target)
    if (info.st_uid, info.st_gid) == owner:
        return
    try:
        os.chown(target, *owner)
    except OSError as exc:
        if info.st_uid == owner[0]:
            # Already the mind's uid: the owner bits are what it reads by, and
            # a group this console is not in is not worth refusing a key for.
            return
        raise OSError(
            exc.errno,
            f"the console may not hand {target.name} to the mind's uid "
            f"{owner[0]}:{owner[1]} ({exc.strerror}); set HIVE_MIND_UID and "
            "HIVE_MIND_GID to the console's own user or run it with that right",
        ) from None


#: A temporary key file the writer leaves only if it was killed mid-write.
_TEMPORARY = re.compile(r"\.key\.[0-9a-f]{12}\.tmp")


#: A temporary key file younger than this may be another writer's, mid-write.
STALE_AFTER_SECONDS = 60.0


def stale_temporaries(directory: Path, *, now: float | None = None) -> int:
    """Delete leftover temporary key files in a mind's folder; how many. Only
    those older than `STALE_AFTER_SECONDS`: a younger one may be a write in
    progress.

    Each holds a key (a replaced one, or the new one before its rename), so a
    killed write must not leave it lying beside the real file."""
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if _TEMPORARY.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink():
            try:
                if (time.time() if now is None else now) - entry.stat().st_mtime < STALE_AFTER_SECONDS:
                    continue
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def write(name: str, value: str) -> Path:
    """Make this the mind's key, atomically. Returns the file's path.

    The value must have the shape of a key: a half-written or garbled value is
    refused here, never written for a mind to find. The folder is the mind's
    to read and nobody else's: owned by the mind's uid with mode 0750 and the
    file 0640 (the folder is mounted into that one mind only; the host user the console runs as has
    to be able to hand the mind its key). Where this console may not chown to
    the mind's uid the write is refused, never loosened to 0755 and 0644."""
    stem = _checked(name)
    if not KEY_SHAPE.fullmatch(str(value or "")):
        raise KeyFileError("that value does not have the shape of a key, so it is not written")
    if not KEY_ROOT.is_dir():
        raise KeyFileError(f"{KEY_ROOT} is not there: this console was given no folder for mind keys")
    directory = KEY_ROOT / stem
    owner = _owner()
    if owner is None:
        raise KeyFileError("HIVE_MIND_UID or HIVE_MIND_GID is not a number, so the key is not written")
    stale_temporaries(directory)
    try:
        directory.mkdir(mode=0o750, exist_ok=True)
        _hand_over(directory, owner)
        os.chmod(directory, 0o750)
    except OSError as exc:
        raise KeyFileError(f"{directory} could not be made ({exc.strerror or exc})") from None
    temporary = directory / f".{FILE_NAME}.{secrets.token_hex(6)}.tmp"
    target = directory / FILE_NAME
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _hand_over(temporary, owner)
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise KeyFileError(f"{target} could not be written ({exc.strerror or exc})") from None
    _fsync_directory(directory)
    return target


def remove(name: str) -> bool:
    """Take this mind's key file, any leftover temporary key file, and its
    folder away. True when a key file was there.

    Used when a mind is unmade: a key left behind in a folder nothing mounts is
    still a key on the host. A folder holding anything else is left."""
    stem = _checked(name)
    directory = KEY_ROOT / stem
    gone = False
    try:
        (directory / FILE_NAME).unlink()
        gone = True
    except FileNotFoundError:
        pass
    stale_temporaries(directory, now=float('inf'))
    try:
        directory.rmdir()
    except (FileNotFoundError, OSError):
        pass
    return gone
