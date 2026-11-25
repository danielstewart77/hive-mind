"""Tests for the page-three call-graph trim.

Each test hands the trim a graph written out in full and asserts the exact
list of rows it returns, in order. Exact equality is the point: "every
changed function plus a neighbour each side" is also satisfied by a trim
that returns the whole module, and only a literal expected list excludes
that.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import design_graph


def _node(name, calls=(), changed=False, **extra):
    return {"name": name, "calls": list(calls), "changed": changed, **extra}


def test_a_straight_chain_shows_one_unchanged_function_at_each_end():
    graph = [
        _node("a", ["b"]),
        _node("b", ["c"], changed=True),
        _node("c", ["d"], changed=True),
        _node("d", ["e"], changed=True),
        _node("e", ["f"]),
        _node("f"),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == ["a", "b", "c", "d", "e"]
    assert [row["role"] for row in rows] == [
        "context",
        "changed",
        "changed",
        "changed",
        "context",
    ]


def test_a_caller_two_hops_above_the_change_is_not_shown():
    graph = [
        _node("top", ["a"]),
        _node("a", ["b"]),
        _node("b", changed=True),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == ["a", "b"]


def test_a_branching_change_set_gets_a_neighbour_below_each_leaf():
    graph = [
        _node("route", ["handler"]),
        _node("handler", ["left", "right"], changed=True),
        _node("left", ["store"], changed=True),
        _node("right", ["notify"], changed=True),
        _node("store"),
        _node("notify"),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == [
        "route",
        "handler",
        "left",
        "store",
        "right",
        "notify",
    ]
    assert {row["name"] for row in rows if row["role"] == "context"} == {
        "route",
        "store",
        "notify",
    }


def test_two_separate_change_sets_each_get_their_own_caller_above():
    graph = [
        _node("route_one", ["first"]),
        _node("first", changed=True),
        _node("route_two", ["second"]),
        _node("second", changed=True),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == [
        "route_one",
        "first",
        "route_two",
        "second",
    ]


def test_an_entry_point_with_no_caller_shows_no_neighbour_above_it():
    graph = [
        _node("handler", ["store"], changed=True),
        _node("store"),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == ["handler", "store"]
    assert design_graph.entry_points_without_callers(graph) == ["handler"]


def test_only_the_first_of_several_unchanged_callers_is_carried():
    graph = [
        _node("preferred", ["target"]),
        _node("also_calls_it", ["target"]),
        _node("target", changed=True),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == ["preferred", "target"]


def test_a_graph_with_nothing_changed_shows_nothing():
    graph = [_node("a", ["b"]), _node("b")]

    assert design_graph.trim_call_graph(graph) == []


def test_each_row_carries_its_file_parameters_and_arguments():
    graph = [
        _node("route", ["handler"]),
        _node(
            "handler",
            ["store"],
            changed=True,
            file="src/main.py",
            repo="/repo",
            params="design_id: str, page: str",
            args="store(design_id)",
        ),
        _node("store"),
    ]

    rows = design_graph.trim_call_graph(graph)
    handler = next(row for row in rows if row["name"] == "handler")

    assert handler["file"] == "src/main.py"
    assert handler["repo"] == "/repo"
    assert handler["params"] == "design_id: str, page: str"
    assert handler["args"] == "store(design_id)"
    assert handler["calls"] == ["store"]
    assert handler["called_by"] == ["route"]


def test_a_callee_with_no_node_of_its_own_is_still_shown():
    """The ordinary case: a mind names the helper its leaf calls without
    bothering to describe it. Indexing that name would crash the trim."""
    graph = [_node("handler", ["a_helper_nobody_described"], changed=True)]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == ["handler", "a_helper_nobody_described"]


def test_a_cyclic_change_set_still_shows_its_neighbours():
    graph = [
        _node("route", ["a"]),
        _node("a", ["b"], changed=True),
        _node("b", ["a", "sink"], changed=True),
        _node("sink"),
    ]

    rows = design_graph.trim_call_graph(graph)

    assert [row["name"] for row in rows] == ["route", "a", "b", "sink"]
