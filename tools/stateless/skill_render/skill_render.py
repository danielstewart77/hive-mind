#!/usr/bin/env python3
"""Render this mind's reference skills and agents into every harness.

Standalone stateless tool over `skill_reference`. Called by each harness's
Stop hook, at mind start, and by comms before a harness switch, so a copy
edited in place under one harness reaches the others before the next turn
runs on them.

    skill_render.py check                      merge in-place edits, render all
    skill_render.py adopt --kind skill --name N --harness claude
    skill_render.py remove --kind agent --name N

Prints the outcome as JSON. Exits 0 when the pass ran, conflicts and
refusals included — those are reported, and notified, not raised — and 1
when it could not run at all.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from minds import skill_reference  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mind", default=None, help="Mind name (default: $MIND_NAME)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Merge in-place edits and render every reference")
    for name in ("adopt", "remove"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--kind", choices=skill_reference.KINDS, required=True)
        cmd.add_argument("--name", required=True)
        if name == "adopt":
            cmd.add_argument("--harness", choices=skill_reference.HARNESSES, required=True)
    args = parser.parse_args(argv)

    try:
        catalog = skill_reference.proxy_catalog(args.mind)
        notify = skill_reference.telegram_notifier()
        if args.command == "check":
            result = skill_reference.check(
                catalog=catalog, notify=notify, mind_name=args.mind
            ).as_dict()
        elif args.command == "adopt":
            result = skill_reference.adopt(
                args.kind, args.name, args.harness,
                catalog=catalog, notify=notify, mind_name=args.mind,
            ).as_dict()
        else:
            skill_reference.remove(args.kind, args.name, mind_name=args.mind)
            result = {"removed": f"{args.kind} {args.name}"}
    except (skill_reference.RenderError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
