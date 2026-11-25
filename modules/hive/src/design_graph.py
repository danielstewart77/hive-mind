"""Trimming a call graph down to the part of it Daniel is being asked about.

Page three answers one question: what is changing, and what does it sit
between. Followed all the way up, a call chain in this hive ends at a
uvicorn worker or a bash hook, which tells a reader nothing; followed all
the way down it ends in the standard library. So the page carries every
function being added or modified, one unchanged function above each place
the chain starts, and one unchanged function below each place it ends.

Two things the naive reading of that rule gets wrong.

A real entry point often has **no caller in the repository at all** — a
FastAPI route handler, a hook script, a `main`. "Exactly one caller above"
cannot be satisfied there, and a page that invents one is worse than a page
that shows none. So it is *at most* one, and its absence is reported rather
than filled.

Where several unchanged callers exist, the graph does not say which one
matters — the mind does, and it says so by the order it lists them in. The
trim takes the first and carries no more, which is what keeps a five-function
change at seven rows instead of thirty.
"""

from __future__ import annotations

from typing import Any, Iterable

CHANGED = "changed"
CONTEXT = "context"


def _index(nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(node["name"]): node for node in nodes}


def _callees(node: dict[str, Any]) -> list[str]:
    return [str(name) for name in (node.get("calls") or [])]


def _callers_of(name: str, nodes: dict[str, dict[str, Any]]) -> list[str]:
    """Who calls this, in the order the mind listed them.

    Derived from the `calls` edges rather than read from a second field, so
    a graph cannot disagree with itself about which way an edge points.
    """
    return [other for other, node in nodes.items() if name in _callees(node)]


def trim_call_graph(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows page three shows, in call order.

    Walked with a visited set: a change set containing mutual recursion has
    no "call order" at all, and the obvious recursive walk loops forever on
    it rather than rendering a page.
    """
    indexed = _index(nodes)
    # Order comes from the list the mind supplied, never from a set: a graph
    # with no entry point — mutual recursion — otherwise renders in whatever
    # order the hash seed produced that morning.
    order = [str(node["name"]) for node in nodes]
    changed = {name for name, node in indexed.items() if node.get("changed")}
    if not changed:
        return []

    entries = [
        name
        for name in order
        if name in changed
        and not any(caller in changed for caller in _callers_of(name, indexed))
    ]
    leaves = {
        name
        for name in changed
        if not any(callee in changed for callee in _callees(indexed[name]))
    }
    if not leaves:
        # A cycle again: every changed function calls another changed one, so
        # nothing looks like the bottom. Any changed function that also calls
        # something unchanged is where the chain leaves the change set, which
        # is what the neighbour below is for.
        leaves = {
            name
            for name in changed
            if any(callee not in changed for callee in _callees(indexed[name]))
        }

    if not entries:
        # Every changed function has a changed caller — mutual recursion, or
        # a cycle. There is no entry point to hang the neighbour off, so the
        # rule falls back to "any changed function an unchanged one calls",
        # which is what a reader means by the top of the chain anyway.
        entries = [
            name
            for name in order
            if name in changed
            and any(caller not in changed for caller in _callers_of(name, indexed))
        ]

    keep: dict[str, str] = {}
    for entry in entries:
        for caller in _callers_of(entry, indexed):
            if caller not in changed:
                keep[caller] = CONTEXT
                break
    for leaf in leaves:
        for callee in _callees(indexed[leaf]):
            if callee not in changed:
                keep[callee] = CONTEXT
                break

    ordered: list[str] = []
    seen: set[str] = set()

    def walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        if name in changed or name in keep:
            ordered.append(name)
        else:
            return
        for callee in _callees(indexed.get(name, {})):
            if callee in changed or callee in keep:
                walk(callee)

    for entry in entries:
        for caller, role in keep.items():
            # A mind naming an unchanged callee without also describing it is
            # the ordinary case — a helper, a stdlib call — so a name with no
            # node behind it is context to show, never a key to index.
            if role == CONTEXT and entry in _callees(indexed.get(caller, {})):
                walk(caller)
        walk(entry)
    for name in order:
        if name in changed:
            walk(name)

    rows = []
    for name in ordered:
        node = indexed.get(name, {"name": name})
        rows.append(
            {
                "name": name,
                "role": CHANGED if name in changed else CONTEXT,
                "file": node.get("file", ""),
                "repo": node.get("repo", ""),
                "params": node.get("params", ""),
                "args": node.get("args", ""),
                "calls": _callees(node),
                "called_by": _callers_of(name, indexed),
            }
        )
    return rows


def entry_points_without_callers(nodes: list[dict[str, Any]]) -> list[str]:
    """Changed functions nothing in the graph calls.

    Named rather than silently shown with a blank neighbour: "nothing here
    calls this" and "I did not look" are different sentences, and a page
    that cannot tell them apart teaches its reader to distrust the blanks.
    """
    indexed = _index(nodes)
    changed = {name for name, node in indexed.items() if node.get("changed")}
    return sorted(
        name
        for name in changed
        if not _callers_of(name, indexed)
    )
