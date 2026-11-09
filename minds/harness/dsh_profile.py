"""Write a mind's dsh profile: ``<minds>/<mind>/.dsh/profiles/hive``.

``minds.harness.dsh_cli`` runs every turn as ``dsh --profile hive`` with
``DSH_HOME`` set to the mind's own ``.dsh``, so a dsh mind has no runner until
this directory exists. It holds what dsh's profile loader reads and nothing it
writes for itself:

* ``package.json`` — the profile manifest: the bundle layers, ``dsh-base``
  then the hive's resumable surface, in that order.
* ``cordis.patch.yml`` — the mind's own patch layer, empty.
* ``cordis.yml`` — the empty root the launcher composes the layers over.
* ``node_modules/<package>`` — an absolute symlink into the dsh tree as the
  mind sees it, for each package in `LINKS`. The launcher resolves the
  plugins its profiles name from the profile directory and from
  ``profiles/node_modules``, which it fills with the dependency closure of its
  own app; the two linked here are outside that closure. The hive bundle is
  not a dependency of the app at all, and the Claude Code hook bridge the
  bundle mounts is only its peer — without that link every mind with a hooks
  file fails its boot with ``ERR_MODULE_NOT_FOUND``. The links dangle on the
  host and resolve inside the container, which is where they are read.

``profiles/node_modules``, the flat fallback beside the profile, is not
written: the launcher rebuilds it from the tree it runs on, every boot.

Existing files are kept, as dsh's own ``initProfile`` keeps them: the patch
layer is the operator's to edit, and a second run must not undo the edit. The
links alone are put right when they point somewhere else, because a link into
another tree is a mind running a harness nobody installed for it.

Standard library only: the installer runs this on a host with no packages::

    python3 -m minds.harness.dsh_profile --mind NAME --out MINDS_DIR [--dsh-root /opt/dsh]

It prints one JSON object naming what it wrote.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

#: The profile every dsh mind's spawn names (``dsh_cli.DSH_PROFILE``).
PROFILE_NAME = "hive"

#: Where a mind container mounts the built dsh tree.
DEFAULT_DSH_ROOT = "/opt/dsh"

#: The hive's own bundle.
HIVE_BUNDLE = "@hive/dsh-headless-resumable"

#: Package name -> its directory in the tree, for every plugin the profile
#: loads that the launcher's own fallback does not provide.
LINKS = {
    HIVE_BUNDLE: "hive/dsh-headless-resumable",
    "@deepseek-ai/dsh-hooks-claude-code": "packages/hooks/hooks-claude-code",
}

#: Bundle layers in application order: the hive surface patches over dsh-base.
BUNDLES = ("@deepseek-ai/dsh-base", HIVE_BUNDLE)

#: The launcher inside the tree, which a container names as ``DSH_BIN``.
LAUNCHER_PATH = "apps/cli/lib/bin.js"

#: One path segment, the shape a mind's folder name takes.
_MIND_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

_ROOT_CONFIG = """\
# dsh profile root — an empty entry list. The tree is composed as patches:
# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any
# --patch overlays. Edit cordis.patch.yml, not this file.
[]
"""

_PATCH_LAYER = """\
# The hive profile: the hive bundle over dsh-base, and nothing else.
#
# It declares no provider and no model. A turn names its model; the one
# provider every model is reached through, the hive inference proxy, lives in
# the bundle, and the conversation's opening model arrives on the spawn's
# environment. This file is this mind's own patch layer, applied after every
# bundle.
[]
"""


def _manifest() -> str:
    manifest = {
        "name": f"dsh-profile-{PROFILE_NAME}",
        "private": True,
        "dsh": {"profile": {"bundles": list(BUNDLES)}},
    }
    return json.dumps(manifest, indent=2) + "\n"


def profile_dir(mind: str, minds_dir: Path) -> Path:
    """The profile directory for this mind under a minds folder."""
    if not _MIND_NAME.fullmatch(mind):
        raise ValueError(f"not a mind name: {mind!r}")
    return Path(minds_dir) / mind / ".dsh" / "profiles" / PROFILE_NAME


def _write_once(path: Path, text: str) -> None:
    if not path.exists():
        path.write_text(text)


def _link(link: Path, target: str) -> None:
    if link.is_symlink():
        if os.readlink(link) == target:
            return
        link.unlink()
    elif link.exists():
        raise FileExistsError(f"{link} exists and is not a symlink")
    link.symlink_to(target)


def write_profile(mind: str, minds_dir: Path, dsh_root: str = DEFAULT_DSH_ROOT) -> dict:
    """Write the mind's hive profile for a tree mounted at ``dsh_root``.

    Returns what was written: the profile directory, the mind's dsh home
    (its ``DSH_HOME``), each link with its target, and the launcher.
    """
    if not dsh_root.startswith("/"):
        raise ValueError(f"the dsh root must be absolute: {dsh_root!r}")
    root = dsh_root.rstrip("/") or "/"
    directory = profile_dir(mind, minds_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_once(directory / "package.json", _manifest())
    _write_once(directory / "cordis.patch.yml", _PATCH_LAYER)
    _write_once(directory / "cordis.yml", _ROOT_CONFIG)
    links = {}
    for package, path in LINKS.items():
        link = directory / "node_modules" / package
        link.parent.mkdir(parents=True, exist_ok=True)
        target = f"{root}/{path}"
        _link(link, target)
        links[str(link)] = target
    return {
        "mind": mind,
        "profile": PROFILE_NAME,
        "profile_dir": str(directory),
        "dsh_home": str(directory.parent.parent),
        "links": links,
        "dsh_bin": f"{root}/{LAUNCHER_PATH}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m minds.harness.dsh_profile",
        description="Write <out>/<mind>/.dsh/profiles/hive for a dsh tree at --dsh-root.",
    )
    parser.add_argument("--mind", required=True, help="the mind's folder name")
    parser.add_argument("--out", required=True, type=Path,
                        help="the minds folder the mind's own folder sits in")
    parser.add_argument("--dsh-root", default=DEFAULT_DSH_ROOT,
                        help="where the mind's container mounts the dsh tree")
    args = parser.parse_args(argv)
    try:
        result = write_profile(args.mind, args.out, args.dsh_root)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
