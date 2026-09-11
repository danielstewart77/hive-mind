"""The dashboard's own logic, below the routes.

Four things live here that the page cannot be trusted to get right in a
browser, and that a route test would only reach through mocks:

- the column cap, which decides *which* conversations are on screen
- host deduplication, because two minds share this workstation
- the three ways a mind can fail to report, which are three different
  remedies applied to three different machines
- one shared, single-flight snapshot, because the alternative is every
  open tab spawning `nvidia-smi` on a child's PC twice a second
"""

from __future__ import annotations

import asyncio

import pytest

import dashboard_data as dd


def _live(*ids, started=None):
    started = started or {}
    return [
        {"session_id": sid, "mind_id": f"mind-{sid}", "started_at": started.get(sid, i)}
        for i, sid in enumerate(ids)
    ]


# --- Requirements 1, 3: every generating mind, named -----------------------


class TestColumns:
    def test_every_generating_conversation_gets_a_column(self):
        """No cap. Six minds producing output are six columns — a slot limit
        would hide the exact thing this page is for."""
        columns = dd.ordered_columns(_live("a", "b", "c", "d", "e", "f"))

        assert [row["session_id"] for row in columns] == [
            "a", "b", "c", "d", "e", "f",
        ]

    def test_columns_are_ordered_oldest_first_and_stay_put(self):
        """Stable by start time on purpose. An arrangement that reshuffled
        each poll would move a conversation mid-sentence."""
        live = _live("new", "old", "middle", started={"old": 1, "middle": 5, "new": 9})

        assert [row["session_id"] for row in dd.ordered_columns(live)] == [
            "old", "middle", "new",
        ]

    def test_a_column_is_headed_by_its_mind_name(self):
        columns = dd.name_columns(
            [{"session_id": "s1", "mind_id": "uuid-1"}],
            [{"id": "uuid-1", "name": "skippy"}],
        )

        assert columns[0]["mind_name"] == "skippy"

    def test_a_mind_the_registry_does_not_know_keeps_its_id(self):
        """Blank is useless — the id is the only handle left on a live
        conversation whose mind the broker has never registered."""
        columns = dd.name_columns(
            [{"session_id": "s1", "mind_id": "uuid-unregistered"}],
            [{"id": "uuid-1", "name": "skippy"}],
        )

        assert columns[0]["mind_name"] == "uuid-unregistered"


# --- Requirement 10: allowance bands ---------------------------------------


class TestPressureBands:
    def test_below_the_warn_line_reads_ok(self):
        assert dd.pressure_band(59.0) == "ok"

    def test_the_warn_line_itself_reads_warn(self):
        assert dd.pressure_band(60.0) == "warn"

    def test_the_top_of_the_warn_band_still_reads_warn(self):
        assert dd.pressure_band(85.0) == "warn"

    def test_past_the_hot_line_reads_hot(self):
        assert dd.pressure_band(86.0) == "hot"

    def test_an_unmeasured_figure_has_no_band(self):
        assert dd.pressure_band(None) is None

    def test_a_reporting_window_carries_its_band(self):
        annotated = dd.annotate_providers(
            {"providers": [
                {"name": "anthropic", "reporting": True,
                 "windows": [{"label": "5h", "used_percent": 91.0}]},
            ]}
        )

        assert annotated["providers"][0]["windows"][0]["band"] == "hot"

    def test_a_provider_reporting_nothing_carries_no_windows(self):
        """Requirement 11: nothing to draw. An unfilled ring reads as
        headroom, which is the opposite of nobody having measured."""
        annotated = dd.annotate_providers(
            {"providers": [
                {"name": "openai", "reporting": False,
                 "windows": [{"label": "5h", "used_percent": 0.0}]},
            ]}
        )

        assert annotated["providers"][0]["windows"] == []


# --- Requirement 25: one machine, one row ----------------------------------


class TestHostDeduplication:
    def test_two_minds_on_one_machine_collapse_to_a_single_host(self):
        """Skippy runs bare metal on this workstation and Mordecai runs in a
        container on it. Two rows would double the machine and draw the
        container's image layer as its disk."""
        reports = [
            {"mind": "skippy", "status": "reporting",
             "reading": {"host": {"host_id": "boot-1", "hostname": "workstation"}}},
            {"mind": "mordecai", "status": "reporting",
             "reading": {"host": {"host_id": "boot-1", "hostname": "a3f91c2b"}}},
        ]

        hosts = dd.merge_hosts(reports)

        assert len(hosts) == 1
        assert sorted(hosts[0]["minds"]) == ["mordecai", "skippy"]

    def test_two_machines_stay_two_rows(self):
        reports = [
            {"mind": "skippy", "status": "reporting",
             "reading": {"host": {"host_id": "boot-1", "hostname": "workstation"}}},
            {"mind": "arnold", "status": "reporting",
             "reading": {"host": {"host_id": "ARNOLD-PC", "hostname": "ARNOLD-PC"}}},
        ]

        assert len(dd.merge_hosts(reports)) == 2

    def test_the_bare_metal_reading_wins_over_the_containers(self):
        """A container's root filesystem is its image. When both minds on a
        machine report, the one that can see the real disk is the one whose
        numbers go on the page."""
        reports = [
            {"mind": "mordecai", "status": "reporting", "reading": {
                "host": {"host_id": "boot-1", "hostname": "a3f91c2b", "containerized": True},
                "disk": {"status": "reporting", "used_bytes": 3, "total_bytes": 10,
                         "reflects_host": False}}},
            {"mind": "skippy", "status": "reporting", "reading": {
                "host": {"host_id": "boot-1", "hostname": "workstation", "containerized": False},
                "disk": {"status": "reporting", "used_bytes": 900, "total_bytes": 4000,
                         "reflects_host": True}}},
        ]

        host = dd.merge_hosts(reports)[0]

        assert host["disk"]["total_bytes"] == 4000

    def test_a_container_alone_on_a_machine_still_reports_and_says_so(self):
        """Half a truth beats a hole. The figure is drawn, flagged as the
        container's own filesystem rather than the host's."""
        reports = [
            {"mind": "mordecai", "status": "reporting", "reading": {
                "host": {"host_id": "boot-9", "hostname": "c1", "containerized": True},
                "disk": {"status": "reporting", "used_bytes": 3, "total_bytes": 10,
                         "reflects_host": False}}},
        ]

        host = dd.merge_hosts(reports)[0]

        assert host["disk"]["reflects_host"] is False
        assert host["disk"]["total_bytes"] == 10


# --- Requirement 16 and 17: distinct failures ------------------------------


class TestFailuresStayDistinct:
    def test_a_refused_mind_is_not_reported_as_unreachable(self):
        """This repo's own rule: a 401 or 503 is never folded into the shape
        that means a machine is down. Labelling a healthy mind unreachable
        sends somebody to reboot a box whose only problem is a token."""
        assert dd.host_status_for(401) == "refused"
        assert dd.host_status_for(503) == "refused"

    def test_a_mind_running_an_older_build_says_so_rather_than_looking_broken(self):
        """A 404 means this mind has no host route yet — a deploy away, not a
        fault. Reading it as unreachable starts a hardware investigation."""
        assert dd.host_status_for(404) == "unsupported"

    def test_a_reachable_mind_is_reporting(self):
        assert dd.host_status_for(200) == "reporting"

    def test_a_host_that_cannot_be_reached_is_named_rather_than_dropped(self):
        reports = [
            {"mind": "zack", "status": "unreachable", "reading": None, "mind_id": "z"},
        ]

        hosts = dd.merge_hosts(reports)

        assert len(hosts) == 1
        assert hosts[0]["status"] == "unreachable"

    def test_an_unreachable_host_carries_no_numbers_to_be_misread(self):
        reports = [{"mind": "zack", "status": "unreachable", "reading": None}]

        host = dd.merge_hosts(reports)[0]

        assert host.get("memory") is None
        assert host.get("disk") is None

    def test_a_failed_mind_does_not_remove_the_others_figures(self):
        reports = [
            {"mind": "zack", "status": "unreachable", "reading": None},
            {"mind": "skippy", "status": "reporting", "reading": {
                "host": {"host_id": "boot-1", "hostname": "workstation"},
                "memory": {"status": "reporting", "used_bytes": 5, "total_bytes": 9}}},
        ]

        hosts = {h["status"]: h for h in dd.merge_hosts(reports)}

        assert hosts["reporting"]["memory"]["used_bytes"] == 5


# --- Requirements 19, 20, 26: one shared, single-flight snapshot ------------


class _Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class TestSharedSnapshot:
    def test_two_reads_inside_the_window_query_once(self):
        clock = _Clock()
        calls = []

        async def fetch():
            calls.append(clock())
            return {"n": len(calls)}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            await snapshot.get()
            clock.now = 1.0
            await snapshot.get()

        asyncio.run(scenario())
        assert len(calls) == 1

    def test_a_read_past_the_window_queries_again(self):
        clock = _Clock()
        calls = []

        async def fetch():
            calls.append(clock())
            return {"n": len(calls)}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            await snapshot.get()
            clock.now = 6.0
            await snapshot.get()

        asyncio.run(scenario())
        assert len(calls) == 2

    def test_the_refreshed_read_returns_the_new_value_not_the_cached_one(self):
        """A second call proves a query happened; it does not prove the page
        shows what came back."""
        clock = _Clock()
        counter = {"n": 0}

        async def fetch():
            counter["n"] += 1
            return {"value": counter["n"]}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            first = await snapshot.get()
            clock.now = 6.0
            return first, await snapshot.get()

        first, second = asyncio.run(scenario())
        assert first["value"] == 1
        assert second["value"] == 2

    def test_simultaneous_readers_share_one_query(self):
        """Requirement 26. Ten tabs opening at once must be one probe of each
        machine, not ten — on the kid boxes that is the difference between a
        dashboard and a denial of service against a laptop."""
        clock = _Clock()
        calls = []

        async def fetch():
            calls.append(1)
            await asyncio.sleep(0)
            return {"n": len(calls)}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            await asyncio.gather(*[snapshot.get() for _ in range(10)])

        asyncio.run(scenario())
        assert len(calls) == 1

    def test_the_reading_carries_the_moment_it_was_taken(self):
        """Requirement 27. A page whose refresh quietly started failing must
        age rather than keep presenting the same numbers as current."""
        clock = _Clock(now=1_700_000_000.0)

        async def fetch():
            return {"ok": True}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)
        result = asyncio.run(snapshot.get())

        assert result["observed_at"] == 1_700_000_000.0

    def test_a_failed_refresh_keeps_the_last_reading_and_marks_it(self):
        """Blanking the page because one poll failed is worse than saying the
        numbers are from a minute ago — but presenting them as fresh is worse
        than both."""
        clock = _Clock()
        state = {"fail": False}

        async def fetch():
            if state["fail"]:
                raise RuntimeError("comms is down")
            return {"value": 1}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            await snapshot.get()
            state["fail"] = True
            clock.now = 6.0
            return await snapshot.get()

        result = asyncio.run(scenario())
        assert result["value"] == 1
        assert result["stale"] is True
        assert "comms is down" in result["error"]

    def test_a_first_read_that_fails_reports_the_failure_rather_than_empty(self):
        """There is no last-known value to fall back to, and an empty payload
        would render as a quiet hive rather than as a broken gateway."""
        clock = _Clock()

        async def fetch():
            raise RuntimeError("comms is down")

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)
        result = asyncio.run(snapshot.get())

        assert result["stale"] is True
        assert result.get("error")


# --- Requirements 8, 9, 10: the context reading ----------------------------


class TestContextReading:
    def test_a_count_against_its_threshold_reads_as_progress(self):
        reading = dd.context_reading(
            {"tokens": 150_000, "threshold": 300_000, "window": 1_000_000}
        )

        assert reading["fraction_of_threshold"] == 0.5

    def test_a_mind_reporting_an_unusual_threshold_is_taken_at_its_word(self):
        """Requirement 10 as amended. The threshold is whatever the mind
        reports — it is read off the spawn arguments, not derived from the
        model — so a figure no lookup table could hold proves nothing was
        substituted."""
        reading = dd.context_reading({"tokens": 68_500, "threshold": 137_000})

        assert reading["fraction_of_threshold"] == 0.5
        assert reading["threshold"] == 137_000

    def test_an_absent_count_is_unknown_rather_than_empty(self):
        """Requirement 7's absent case, and the one that matters: a bar drawn
        at zero for a conversation actually at 138k reads as plenty of room."""
        reading = dd.context_reading({"tokens": None, "threshold": 300_000})

        assert reading["status"] == "unknown"
        assert reading["fraction_of_threshold"] is None

    def test_an_absent_threshold_leaves_the_progress_unknown(self):
        reading = dd.context_reading({"tokens": 150_000, "threshold": None})

        assert reading["fraction_of_threshold"] is None
        assert reading["tokens"] == 150_000

    def test_a_threshold_of_zero_does_not_divide(self):
        reading = dd.context_reading({"tokens": 10, "threshold": 0})

        assert reading["fraction_of_threshold"] is None

    def test_a_count_past_its_threshold_is_reported_past_it_not_clamped(self):
        """A rotation fires on the next typed message, which can be hours
        away, so sitting over the line is a real and visible state. Clamping
        to 100% would hide the conversation that most needs attention."""
        reading = dd.context_reading({"tokens": 330_000, "threshold": 300_000})

        assert reading["fraction_of_threshold"] == pytest.approx(1.1)
        assert reading["over"] is True

    def test_the_window_is_carried_so_the_threshold_reads_against_it(self):
        reading = dd.context_reading(
            {"tokens": 150_000, "threshold": 300_000, "window": 1_000_000}
        )

        assert reading["window"] == 1_000_000
        assert reading["fraction_of_window"] == 0.15

    def test_an_undeclared_window_is_unknown_rather_than_assumed(self):
        """Requirement 10c. Inventing a denominator draws a conversation at
        95% as one at 40%."""
        reading = dd.context_reading({"tokens": 150_000, "threshold": 300_000})

        assert reading["window"] is None
        assert reading["fraction_of_window"] is None


# --- Requirement 9: the window comes from the model map --------------------


class TestWindowLookup:
    def test_a_session_takes_the_window_its_model_declares(self):
        catalog = {"claude-opus-5": 1_000_000, "gpt-5.6": 400_000}

        assert dd.window_for("claude-opus-5", catalog) == 1_000_000

    def test_a_model_the_catalog_does_not_know_yields_no_window(self):
        assert dd.window_for("house-model-7", {"claude-opus-5": 1_000_000}) is None

    def test_a_model_with_a_null_window_yields_no_window(self):
        assert dd.window_for("gpt-5.6", {"gpt-5.6": None}) is None

    def test_what_the_mind_reports_beats_the_catalog(self):
        """The catalog holds the deployment's window; the mind holds the
        running process's. A Claude conversation spawned without the long
        pin has 200k on a row that says a million, and the mind is the one
        that can tell."""
        assert dd.resolve_window(reported=200_000, catalog_window=1_000_000) == 200_000

    def test_the_catalog_fills_in_when_the_mind_reported_nothing(self):
        assert dd.resolve_window(reported=None, catalog_window=1_000_000) == 1_000_000


class TestZeroIsAMeasurement:
    def test_a_conversation_genuinely_at_zero_reports_zero(self):
        """A conversation that has just started really is at zero, and that
        is a reading. Only absence is unknown — a falsy check collapses the
        two and the panel can no longer say which it is looking at."""
        reading = dd.context_reading({"tokens": 0, "threshold": 300_000})

        assert reading["status"] == "reporting"
        assert reading["fraction_of_threshold"] == 0.0


class TestEveryFailedMachineKeepsItsOwnRow:
    def test_two_unreachable_machines_are_two_rows(self):
        """Two boxes asleep at once must not merge. A shared fallback key
        hides the second one behind the first, and the machine nobody can
        see is the machine nobody fixes."""
        reports = [
            {"mind": "zack", "status": "unreachable", "reading": None},
            {"mind": "arnold", "status": "unreachable", "reading": None},
        ]

        assert len(dd.merge_hosts(reports)) == 2


class TestAFailingUpstreamIsThrottledToo:
    def test_repeated_failures_inside_the_window_query_once(self):
        """The failing case is the one where hammering hurts most — an
        erroring proxy polled by every tab every five seconds. Stamping the
        window only on success turns that into an unbounded retry loop."""
        clock = _Clock()
        calls = []

        async def fetch():
            calls.append(clock())
            raise RuntimeError("proxy down")

        snapshot = dd.SharedSnapshot(fetch, ttl=60.0, clock=clock)

        async def scenario():
            await snapshot.get()
            clock.now = 30.0
            await snapshot.get()

        asyncio.run(scenario())
        assert len(calls) == 1

    def test_a_failure_past_the_window_tries_again(self):
        clock = _Clock()
        calls = []

        async def fetch():
            calls.append(clock())
            raise RuntimeError("proxy down")

        snapshot = dd.SharedSnapshot(fetch, ttl=60.0, clock=clock)

        async def scenario():
            await snapshot.get()
            clock.now = 61.0
            await snapshot.get()

        asyncio.run(scenario())
        assert len(calls) == 2

    def test_a_recovery_clears_the_error_rather_than_carrying_it(self):
        clock = _Clock()
        state = {"fail": True}

        async def fetch():
            if state["fail"]:
                raise RuntimeError("proxy down")
            return {"value": 7}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            await snapshot.get()
            state["fail"] = False
            clock.now = 6.0
            return await snapshot.get()

        result = asyncio.run(scenario())
        assert result["value"] == 7
        assert result["stale"] is False
        assert result.get("error") is None

    def test_a_cached_failure_still_reports_the_last_good_reading(self):
        clock = _Clock()
        state = {"fail": False}

        async def fetch():
            if state["fail"]:
                raise RuntimeError("proxy down")
            return {"value": 1}

        snapshot = dd.SharedSnapshot(fetch, ttl=5.0, clock=clock)

        async def scenario():
            await snapshot.get()
            state["fail"] = True
            clock.now = 6.0
            await snapshot.get()
            clock.now = 8.0
            return await snapshot.get()

        result = asyncio.run(scenario())
        assert result["value"] == 1
        assert result["stale"] is True


# --- Requirement 7: an absent count is not a count of zero -----------------


class TestUnmeasuredContext:
    def test_a_conversation_with_no_count_reports_no_fill_at_all(self):
        reading = dd.context_reading({"threshold": 300000, "window": 1000000})

        assert reading["status"] == "unknown"
        assert reading["bar_fraction"] is None
        assert reading["fraction_of_window"] is None
        assert reading["fraction_of_threshold"] is None


# --- The bar always means something ----------------------------------------


class TestBarBasis:
    def test_the_bar_is_the_window_when_the_window_is_known(self):
        reading = dd.context_reading(
            {"tokens": 140000, "threshold": 300000, "window": 1000000}
        )

        assert reading["bar_basis"] == "window"
        assert reading["bar_fraction"] == 0.14
        assert reading["mark_fraction"] == 0.3

    def test_an_unknown_window_falls_back_to_the_rotation_point(self):
        """The proxy catalog is legitimately absent — an unreachable proxy,
        or a deployment name it has never been told about. A conversation at
        95% of rotation must not render as one nobody has measured."""
        reading = dd.context_reading({"tokens": 190000, "threshold": 200000})

        assert reading["bar_basis"] == "threshold"
        assert reading["bar_fraction"] == 0.95

    def test_with_neither_denominator_there_is_no_bar_at_all(self):
        reading = dd.context_reading({"tokens": 190000})

        assert reading["bar_basis"] is None
        assert reading["bar_fraction"] is None

    def test_a_rotation_point_of_zero_draws_no_mark(self):
        """A mark at the left edge claims this conversation rotates
        immediately, which is worse than no mark at all."""
        reading = dd.context_reading(
            {"tokens": 5000, "threshold": 0, "window": 1000000}
        )

        assert reading["mark_fraction"] is None

    def test_a_rotation_point_past_the_window_is_flagged_not_clamped(self):
        """A mind reporting a 200k window against a 300k rotation point is
        the dropped [1m] pin. Squared away at the edge it reads as
        'rotates right at the end', which is reassuring and wrong."""
        reading = dd.context_reading(
            {"tokens": 10000, "threshold": 300000, "window": 200000}
        )

        assert reading["mark_beyond_window"] is True

    def test_a_rotation_point_inside_the_window_is_not_flagged(self):
        reading = dd.context_reading(
            {"tokens": 10000, "threshold": 300000, "window": 1000000}
        )

        assert reading["mark_beyond_window"] is False

    def test_overflowing_the_window_with_no_rotation_point_still_reads_over(self):
        """With nothing else to be past, the window is the line. A bar that
        can never turn red is a bar that cannot warn."""
        reading = dd.context_reading({"tokens": 250000, "window": 200000})

        assert reading["over"] is True


class TestProviderShapes:
    def test_a_body_that_is_not_a_list_yields_no_providers(self):
        """A 200 carrying the wrong body is a proxy answering, not a proxy
        down — it must not raise out of the route and take the
        conversations and the machines with it."""
        assert dd.annotate_providers({"providers": None})["providers"] == []

    def test_a_window_whose_figure_is_not_a_number_is_dropped(self):
        annotated = dd.annotate_providers(
            {"providers": [{"reporting": True, "windows": [
                {"label": "5h", "used_percent": "91.0"},
                {"label": "7d", "used_percent": 40.0},
            ]}]}
        )

        assert [w["label"] for w in annotated["providers"][0]["windows"]] == ["7d"]

    def test_a_window_with_no_figure_at_all_is_dropped(self):
        annotated = dd.annotate_providers(
            {"providers": [{"reporting": True, "windows": [{"label": "5h"}]}]}
        )

        assert annotated["providers"][0]["windows"] == []
