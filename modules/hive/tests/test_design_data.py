"""Tests for the design-session store.

Every test here drives `design_data` itself. The store is the whole of the
protection Daniel is buying — a revision that cannot overwrite what he left
alone, a save that cannot clobber another tab, a Build that cannot be sent
twice — so these assert on what it stored, never on what a caller passed in.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import design_data


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    return design_data


def _requirements(*texts: str) -> dict:
    return {
        f"r{index}": {"order": index, "text": text}
        for index, text in enumerate(texts, start=1)
    }


# --- Requirement 1, 2: the list and its lifetime -----------------------------


async def test_listing_reports_title_binding_and_recency(store):
    first = await store.create_design_session("Older design", conversation_id="conv-a")
    second = await store.create_design_session("Newer design", conversation_id="conv-b")

    listed = await store.list_design_sessions()

    assert [row["id"] for row in listed] == [second["id"], first["id"]]
    assert [row["title"] for row in listed] == ["Newer design", "Older design"]
    assert [row["conversation_id"] for row in listed] == ["conv-b", "conv-a"]
    assert listed[0]["updated_at"] >= listed[1]["updated_at"]


async def test_reading_a_design_session_does_not_move_it_up_the_list(store):
    first = await store.create_design_session("First")
    second = await store.create_design_session("Second")

    await store.get_design_session(first["id"])

    assert [row["id"] for row in await store.list_design_sessions()] == [
        second["id"],
        first["id"],
    ]


async def test_delete_removes_the_session(store):
    record = await store.create_design_session("Doomed")

    await store.delete_design_session(record["id"])

    assert await store.list_design_sessions() == []


async def test_delete_of_an_id_the_store_does_not_hold_is_refused(store):
    record = await store.create_design_session("Doomed")
    await store.delete_design_session(record["id"])
    absent = record["id"]
    assert absent not in {row["id"] for row in await store.list_design_sessions()}

    with pytest.raises(store.DesignNotFound):
        await store.delete_design_session(absent)


# --- Requirement 3, 7: re-binding --------------------------------------------


async def test_rebinding_keeps_the_same_record_and_its_content(store):
    record = await store.create_design_session("Design", conversation_id="conv-old")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("The page lists design sessions"),
        revision=record["revision"],
    )

    rebound = await store.attach_design_session(
        record["id"], "conv-new", surface="telegram"
    )

    assert rebound["id"] == record["id"]
    assert rebound["conversation_id"] == "conv-new"
    assert rebound["surface"] == "telegram"
    assert rebound["requirements"] == published["requirements"]


# --- Requirement 8, 9, 10, 12, 13: the requirements loop ----------------------


async def test_published_requirements_read_back_in_display_order(store):
    record = await store.create_design_session("Design")

    published = await store.publish_page(
        record["id"],
        "requirements",
        {
            "r2": {"order": 2, "text": "Second"},
            "r1": {"order": 1, "text": "First"},
        },
        revision=record["revision"],
    )

    assert [item["text"] for item in store.ordered_items(published, "requirements")] == [
        "First",
        "Second",
    ]


async def test_submitting_replaces_the_minds_wording_with_daniels(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )

    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={
            "r1": {"text": "Every design session is listed"},
            "r2": {"feedback": "Does delete remove the notices too?"},
        },
        revision=published["revision"],
    )

    assert submitted["requirements"]["r1"]["text"] == "Every design session is listed"
    assert (
        submitted["requirements"]["r2"]["feedback"]
        == "Does delete remove the notices too?"
    )
    assert submitted["requirements"]["r2"]["text"] == "Sessions can be deleted"


async def test_a_revision_clears_feedback_and_keeps_the_question_beside_its_answer(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "Listed in what order?"}},
        revision=published["revision"],
    )

    revised = await store.publish_page(
        submitted["id"],
        "requirements",
        {
            "r1": {
                "order": 1,
                "text": "Sessions are listed, most recently written first",
                "answer": "Most recently written first.",
            }
        },
        revision=submitted["revision"],
    )

    item = revised["requirements"]["r1"]
    assert item["feedback"] == ""
    assert item["question_answered"] == "Listed in what order?"
    assert item["answer"] == "Most recently written first."
    assert item["text"] == "Sessions are listed, most recently written first"


# --- Requirement 31: a revision may only change what Daniel touched ----------


async def test_a_revision_refuses_to_rewrite_a_requirement_with_no_feedback(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "In what order?"}},
        revision=published["revision"],
    )

    with pytest.raises(store.DesignRefused) as refusal:
        await store.publish_page(
            submitted["id"],
            "requirements",
            {
                "r1": {"order": 1, "text": "Sessions are listed newest first"},
                "r2": {"order": 2, "text": "Sessions can be deleted and archived"},
            },
            revision=submitted["revision"],
        )

    assert refusal.value.keys == ["r2"]
    after = await store.get_design_session(submitted["id"])
    assert after["requirements"]["r2"]["text"] == "Sessions can be deleted"
    assert after["requirements"]["r1"]["text"] == "Sessions are listed"


async def test_a_revision_changes_the_item_that_carries_feedback(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "In what order?"}},
        revision=published["revision"],
    )

    revised = await store.publish_page(
        submitted["id"],
        "requirements",
        {
            "r1": {"order": 1, "text": "Sessions are listed newest first"},
            "r2": {"order": 2, "text": "Sessions can be deleted"},
        },
        revision=submitted["revision"],
    )

    assert revised["requirements"]["r1"]["text"] == "Sessions are listed newest first"
    assert revised["requirements"]["r2"]["text"] == "Sessions can be deleted"


async def test_an_edit_with_no_question_still_lets_the_mind_revise_that_item(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"text": "Every session is listed"}},
        revision=published["revision"],
    )

    revised = await store.publish_page(
        submitted["id"],
        "requirements",
        {"r1": {"order": 1, "text": "Every design session is listed"}},
        revision=submitted["revision"],
    )

    assert revised["requirements"]["r1"]["text"] == "Every design session is listed"


async def test_a_revision_may_not_delete_a_requirement_daniel_left_alone(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )

    revised = await store.publish_page(
        published["id"],
        "requirements",
        {"r1": {"order": 1, "text": "Sessions are listed"}},
        revision=published["revision"],
    )

    assert set(revised["requirements"]) == {"r1", "r2"}


async def test_the_mind_owns_the_tests_page_outright(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session"}},
        revision=record["revision"],
    )

    revised = await store.publish_page(
        published["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session, newest first"}},
        revision=published["revision"],
    )

    assert (
        revised["tests"]["t1"]["sentence"]
        == "Listing reports every session, newest first"
    )


# --- Two writers ------------------------------------------------------------


async def test_a_write_carrying_a_stale_revision_is_refused(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    stale_revision = published["revision"]
    await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "First tab"}},
        revision=stale_revision,
    )

    with pytest.raises(store.DesignConflict):
        await store.submit_page(
            published["id"],
            "requirements",
            items={"r1": {"feedback": "Second tab"}},
            revision=stale_revision,
        )

    after = await store.get_design_session(published["id"])
    assert after["requirements"]["r1"]["feedback"] == "First tab"


async def test_a_write_with_no_revision_is_refused(store):
    record = await store.create_design_session("Design")

    with pytest.raises(store.DesignConflict):
        await store.publish_page(
            record["id"], "requirements", _requirements("Sessions are listed")
        )


async def test_submissions_against_different_items_both_survive(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("One", "Two"),
        revision=record["revision"],
    )

    first = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "About one"}},
        revision=published["revision"],
    )
    second = await store.submit_page(
        first["id"],
        "requirements",
        items={"r2": {"feedback": "About two"}},
        revision=first["revision"],
    )

    assert second["requirements"]["r1"]["feedback"] == "About one"
    assert second["requirements"]["r2"]["feedback"] == "About two"


# --- Staleness --------------------------------------------------------------


async def test_editing_a_requirement_marks_the_later_pages_stale(store):
    record = await store.create_design_session("Design")
    with_reqs = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    with_tests = await store.publish_page(
        with_reqs["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session"}},
        revision=with_reqs["revision"],
    )

    submitted = await store.submit_page(
        with_tests["id"],
        "requirements",
        items={"r1": {"text": "Sessions are listed newest first"}},
        revision=with_tests["revision"],
    )

    assert submitted["stale"]["tests"] is True


async def test_a_question_with_no_wording_change_leaves_the_later_pages_fresh(store):
    record = await store.create_design_session("Design")
    with_reqs = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    with_tests = await store.publish_page(
        with_reqs["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session"}},
        revision=with_reqs["revision"],
    )

    submitted = await store.submit_page(
        with_tests["id"],
        "requirements",
        items={"r1": {"feedback": "Listed in what order?"}},
        revision=with_tests["revision"],
    )

    assert submitted["stale"].get("tests") is not True


async def test_republishing_the_tests_page_clears_its_stale_mark(store):
    record = await store.create_design_session("Design")
    with_reqs = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    with_tests = await store.publish_page(
        with_reqs["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session"}},
        revision=with_reqs["revision"],
    )
    submitted = await store.submit_page(
        with_tests["id"],
        "requirements",
        items={"r1": {"text": "Sessions are listed newest first"}},
        revision=with_tests["revision"],
    )

    republished = await store.publish_page(
        submitted["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session, newest first"}},
        revision=submitted["revision"],
    )

    assert republished["stale"]["tests"] is False


# --- Requirement 18: the tests gate -----------------------------------------


def _resolved_test(name: str, resolved: bool, kind: str = "existing") -> dict:
    return {
        "order": 1,
        "sentence": f"{name} behaves",
        "symbol": {"name": name, "kind": kind, "resolved": resolved},
        "cases": [
            {"input": "a known good value", "expected": "accepted", "outcome": "pass"},
            {"input": "a value the store says is absent", "expected": "refused",
             "outcome": "fail"},
        ],
    }


async def test_a_tests_page_naming_a_function_that_resolved_can_be_accepted(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "tests",
        {"t1": _resolved_test("list_design_sessions", True)},
        revision=record["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is True
    assert reason == ""


async def test_a_tests_page_naming_an_unresolved_function_cannot_be_accepted(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "tests",
        {"t1": _resolved_test("list_desgin_sessions", False)},
        revision=record["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is False
    assert "list_desgin_sessions" in reason
    with pytest.raises(store.DesignError):
        await store.accept_page(published["id"], "tests", revision=published["revision"])


async def test_a_test_naming_a_function_specified_on_page_three_may_be_accepted(store):
    record = await store.create_design_session("Design")
    with_functions = await store.publish_page(
        record["id"],
        "functions",
        {"f1": {"order": 1, "name": "publish_page", "changed": True}},
        revision=record["revision"],
    )
    published = await store.publish_page(
        with_functions["id"],
        "tests",
        {"t1": _resolved_test("publish_page", False, kind="planned")},
        revision=with_functions["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is True
    assert reason == ""


async def test_a_planned_function_absent_from_page_three_still_blocks_the_page(store):
    record = await store.create_design_session("Design")
    with_functions = await store.publish_page(
        record["id"],
        "functions",
        {"f1": {"order": 1, "name": "publish_page", "changed": True}},
        revision=record["revision"],
    )
    published = await store.publish_page(
        with_functions["id"],
        "tests",
        {"t1": _resolved_test("publish_pages", False, kind="planned")},
        revision=with_functions["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is False
    assert "publish_pages" in reason


async def test_a_submission_un_accepts_the_page_it_lands_on(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    accepted = await store.accept_page(
        published["id"], "requirements", revision=published["revision"]
    )
    assert accepted["accepted"]["requirements"] is True

    submitted = await store.submit_page(
        accepted["id"],
        "requirements",
        items={"r1": {"feedback": "Actually, one more thing"}},
        revision=accepted["revision"],
    )

    assert submitted["accepted"]["requirements"] is False


# --- Requirement 25, 30: notices and delivery -------------------------------


async def _built(store, **kwargs):
    """A design session with all three pages published and the first two
    accepted — the state Build is actually allowed from."""
    record = await store.create_design_session("Design", **kwargs)
    record = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    record = await store.publish_page(
        record["id"],
        "functions",
        {"f1": {"order": 1, "name": "publish_page", "changed": True}},
        revision=record["revision"],
    )
    record = await store.publish_page(
        record["id"],
        "tests",
        {"t1": _resolved_test("publish_page", True)},
        revision=record["revision"],
    )
    record = await store.accept_page(
        record["id"], "requirements", revision=record["revision"]
    )
    record = await store.accept_page(record["id"], "tests", revision=record["revision"])
    # Reaching this state sends two acceptances. They are arrangement, not
    # the behaviour any test using this fixture is about, so they are marked
    # delivered here — otherwise every assertion on the notice queue below
    # is really an assertion about how the fixture got built.
    for notice in await store.pending_notices(record["id"]):
        await store.claim_notice(notice["id"])
        await store.mark_delivered(notice["id"])
    return await store.get_design_session(record["id"])


async def test_feedback_and_build_queue_different_notices(store):
    record = await _built(store, conversation_id="conv-a")

    feedback = await store.submit_page(
        record["id"],
        "functions",
        items={"f1": {"feedback": "Why does this call the store twice?"}},
        revision=record["revision"],
    )
    await store.submit_page(
        feedback["id"], "functions", items={}, action="build",
        revision=feedback["revision"],
    )

    kinds = [notice["kind"] for notice in await store.pending_notices(record["id"])]
    assert kinds == ["feedback", "build"]


async def test_build_is_refused_when_no_conversation_is_attached(store):
    record = await _built(store)

    with pytest.raises(store.DesignError):
        await store.submit_page(
            record["id"], "functions", items={}, action="build",
            revision=record["revision"],
        )

    assert await store.pending_notices(record["id"]) == []


async def test_a_notice_can_only_be_claimed_once(store):
    record = await _built(store, conversation_id="conv-a")
    await store.submit_page(
        record["id"], "functions", items={}, action="build",
        revision=record["revision"],
    )
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    claimed = await store.claim_notice(notice_id)
    assert claimed["kind"] == "build"

    with pytest.raises(store.DesignNotFound):
        await store.claim_notice(notice_id)


async def test_a_released_notice_can_be_claimed_again(store):
    record = await _built(store, conversation_id="conv-a")
    await store.submit_page(
        record["id"], "functions", items={}, action="build",
        revision=record["revision"],
    )
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]
    await store.claim_notice(notice_id)

    await store.release_notice(notice_id)

    assert (await store.claim_notice(notice_id))["id"] == notice_id


async def test_a_delivered_notice_stops_being_pending(store):
    record = await _built(store, conversation_id="conv-a")
    await store.submit_page(
        record["id"], "functions", items={}, action="build",
        revision=record["revision"],
    )
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]
    await store.claim_notice(notice_id)

    await store.mark_delivered(notice_id)

    assert await store.pending_notices(record["id"]) == []
    with pytest.raises(store.DesignNotFound):
        await store.mark_delivered(notice_id)


async def test_a_submission_survives_a_conversation_that_no_longer_exists(store):
    record = await _built(store, conversation_id="conv-gone")
    submitted = await store.submit_page(
        record["id"],
        "functions",
        items={"f1": {"feedback": "Asked while the conversation was dead"}},
        revision=record["revision"],
    )
    await store.attach_design_session(submitted["id"], None)

    pending = await store.pending_notices(submitted["id"])

    assert [notice["kind"] for notice in pending] == ["feedback"]
    assert (
        (await store.get_design_session(submitted["id"]))["functions"]["f1"]["feedback"]
        == "Asked while the conversation was dead"
    )


# --- What the code grill found ----------------------------------------------


async def test_two_writers_at_one_revision_cannot_both_win(store, monkeypatch):
    """The revision travels into the UPDATE, so the loser is refused even
    when both callers passed the Python-side check before either wrote."""
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )
    revision = published["revision"]

    real_write = store._write_page
    landed = []

    async def racing_write(design_id, page, items, **kwargs):
        # The other tab lands between this caller's check and its write.
        if not landed:
            landed.append(True)
            await real_write(
                design_id,
                page,
                {"r1": dict(published["requirements"]["r1"],
                            feedback="TAB-ONE")},
                accepted=kwargs["accepted"],
                stale=kwargs["stale"],
                expected_revision=revision,
            )
        return await real_write(design_id, page, items, **kwargs)

    monkeypatch.setattr(store, "_write_page", racing_write)

    with pytest.raises(store.DesignConflict):
        await store.submit_page(
            published["id"],
            "requirements",
            items={"r1": {"feedback": "TAB-TWO"}},
            revision=revision,
        )

    after = await store.get_design_session(published["id"])
    assert after["requirements"]["r1"]["feedback"] == "TAB-ONE"
    assert len(await store.pending_notices(published["id"])) == 0


async def test_a_partial_publish_does_not_delete_the_requirement_daniel_asked_about(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "In what order?"}},
        revision=published["revision"],
    )

    revised = await store.publish_page(
        submitted["id"],
        "requirements",
        {"r1": {"order": 1, "text": "Sessions are listed newest first"}},
        revision=submitted["revision"],
    )

    assert set(revised["requirements"]) == {"r1", "r2"}
    assert revised["requirements"]["r2"]["text"] == "Sessions can be deleted"


async def test_a_flagged_requirement_can_be_deleted_when_the_mind_says_so(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r2": {"feedback": "Drop this one, it is covered by 1"}},
        revision=published["revision"],
    )

    revised = await store.publish_page(
        submitted["id"],
        "requirements",
        {
            "r1": {"order": 1, "text": "Sessions are listed"},
            "r2": {"deleted": True},
        },
        revision=submitted["revision"],
    )

    assert set(revised["requirements"]) == {"r1"}


async def test_deleting_a_requirement_daniel_left_alone_is_refused(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed", "Sessions can be deleted"),
        revision=record["revision"],
    )

    with pytest.raises(store.DesignRefused):
        await store.publish_page(
            published["id"],
            "requirements",
            {
                "r1": {"order": 1, "text": "Sessions are listed"},
                "r2": {"deleted": True},
            },
            revision=published["revision"],
        )

    assert set((await store.get_design_session(published["id"]))["requirements"]) == {"r1", "r2"}


async def test_build_is_refused_while_a_page_is_unaccepted(store):
    record = await _built(store, conversation_id="conv-a")
    reopened = await store.submit_page(
        record["id"],
        "requirements",
        items={"r1": {"feedback": "One more thing"}},
        revision=record["revision"],
    )

    with pytest.raises(store.DesignError):
        await store.submit_page(
            reopened["id"], "functions", items={}, action="build",
            revision=reopened["revision"],
        )


async def test_build_is_refused_while_the_tests_page_is_stale(store):
    """Stale *and* accepted. Editing a requirement also un-accepts page one,
    so re-accepting it is what leaves staleness as the only thing standing —
    without that, the acceptance gate answers and this proves nothing."""
    record = await _built(store, conversation_id="conv-a")
    stale = await store.submit_page(
        record["id"],
        "requirements",
        items={"r1": {"text": "Sessions are listed newest first"}},
        revision=record["revision"],
    )
    assert stale["stale"]["tests"] is True
    reaccepted = await store.accept_page(
        stale["id"], "requirements", revision=stale["revision"]
    )
    assert reaccepted["accepted"]["requirements"] is True
    assert reaccepted["accepted"]["tests"] is True
    assert reaccepted["stale"]["tests"] is True

    with pytest.raises(store.DesignError) as refusal:
        await store.submit_page(
            reaccepted["id"], "functions", items={}, action="build",
            revision=reaccepted["revision"],
        )

    assert "older requirements" in str(refusal.value)


async def test_build_is_refused_while_a_test_names_an_unresolved_function(store):
    record = await _built(store, conversation_id="conv-a")
    broken = await store.publish_page(
        record["id"],
        "tests",
        {"t1": _resolved_test("publsh_page", False)},
        revision=record["revision"],
    )
    # The gate refuses, so acceptance cannot be regained.
    with pytest.raises(store.DesignError):
        await store.accept_page(broken["id"], "tests",
                                revision=broken["revision"])
    with pytest.raises(store.DesignError):
        await store.submit_page(
            broken["id"], "functions", items={}, action="build",
            revision=broken["revision"],
        )


async def test_a_stale_page_cannot_be_accepted(store):
    record = await _built(store, conversation_id="conv-a")
    stale = await store.submit_page(
        record["id"],
        "requirements",
        items={"r1": {"text": "Sessions are listed newest first"}},
        revision=record["revision"],
    )

    with pytest.raises(store.DesignError):
        await store.accept_page(stale["id"], "tests", revision=stale["revision"])


async def test_a_planned_symbol_with_no_name_does_not_pass_the_gate(store):
    record = await store.create_design_session("Design")
    with_functions = await store.publish_page(
        record["id"],
        "functions",
        {"f1": {"order": 1, "name": "", "changed": True}},
        revision=record["revision"],
    )
    published = await store.publish_page(
        with_functions["id"],
        "tests",
        {
            "t1": dict(
                _resolved_test("", False, kind="planned"),
                sentence="something",
            )
        },
        revision=with_functions["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is False
    # Named, so the cases gate cannot be the one answering.
    assert "Unresolved" in reason


async def test_a_windows_line_ending_round_trip_is_not_an_edit(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        {"r1": {"order": 1, "text": "First line\r\nsecond line"}},
        revision=record["revision"],
    )

    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"text": "First line\nsecond line"}},
        revision=published["revision"],
    )

    assert submitted["requirements"]["r1"].get("edited") is not True


async def test_re_attaching_does_not_invalidate_what_daniel_is_typing(store):
    record = await store.create_design_session("Design", conversation_id="conv-a")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )

    await store.attach_design_session(published["id"], "conv-b", surface="telegram")

    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "Typed before the re-claim"}},
        revision=published["revision"],
    )
    assert submitted["requirements"]["r1"]["feedback"] == "Typed before the re-claim"


async def test_a_submission_stores_its_notice_in_the_same_write(store, monkeypatch):
    """A page written with nobody told about it is a submission that vanishes."""
    record = await store.create_design_session("Design", conversation_id="conv-a")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("Sessions are listed"),
        revision=record["revision"],
    )

    await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "A question"}},
        revision=published["revision"],
    )

    assert [n["kind"] for n in await store.pending_notices(published["id"])] == ["feedback"]


async def test_a_requirement_can_be_inserted_between_two_others(store):
    """Position is not content. Splitting one requirement in two moves every
    sibling below it, and refusing that as an unauthorised rewrite is what
    made inserting a requirement impossible."""
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("First", "Second"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "Split this into two"}},
        revision=published["revision"],
    )

    revised = await store.publish_page(
        submitted["id"],
        "requirements",
        {
            "r1": {"order": 1, "text": "First, part one"},
            "r1b": {"order": 2, "text": "First, part two"},
            "r2": {"order": 3, "text": "Second"},
        },
        revision=submitted["revision"],
    )

    assert [item["text"] for item in store.ordered_items(revised, "requirements")] == [
        "First, part one",
        "First, part two",
        "Second",
    ]


async def test_moving_an_untouched_requirement_does_not_change_its_words(store):
    """The exemption is for position alone — it must not become a hole the
    text can travel through."""
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "requirements",
        _requirements("First", "Second"),
        revision=record["revision"],
    )
    submitted = await store.submit_page(
        published["id"],
        "requirements",
        items={"r1": {"feedback": "Split this"}},
        revision=published["revision"],
    )

    with pytest.raises(store.DesignRefused) as refusal:
        await store.publish_page(
            submitted["id"],
            "requirements",
            {
                "r1": {"order": 1, "text": "First, revised"},
                "r2": {"order": 3, "text": "Second, quietly reworded"},
            },
            revision=submitted["revision"],
        )

    assert refusal.value.keys == ["r2"]


async def test_a_test_with_only_a_passing_case_does_not_pass_the_gate(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "tests",
        {
            "t1": {
                "order": 1,
                "sentence": "Listing reports every session",
                "symbol": {"name": "list_design_sessions", "kind": "existing",
                           "resolved": True},
                "cases": [
                    {"input": "two stored", "expected": "both returned",
                     "outcome": "pass"}
                ],
            }
        },
        revision=record["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is False
    assert "succeed" in reason and "fail" in reason


async def test_a_test_with_both_a_passing_and_a_failing_case_passes_the_gate(store):
    record = await store.create_design_session("Design")
    published = await store.publish_page(
        record["id"],
        "tests",
        {"t1": _resolved_test("list_design_sessions", True)},
        revision=record["revision"],
    )

    allowed, reason = await store.can_advance(published["id"], "tests")

    assert allowed is True
    assert reason == ""


async def test_build_pressed_from_the_wrong_page_is_refused(store):
    record = await _built(store, conversation_id="conv-a")

    with pytest.raises(store.DesignError):
        await store.submit_page(
            record["id"], "requirements", items={}, action="build",
            revision=record["revision"],
        )

    assert await store.pending_notices(record["id"]) == []


async def test_an_action_that_is_neither_feedback_nor_build_is_refused(store):
    record = await _built(store, conversation_id="conv-a")

    with pytest.raises(store.DesignError):
        await store.submit_page(
            record["id"], "functions", items={}, action="ship-it",
            revision=record["revision"],
        )

    assert await store.pending_notices(record["id"]) == []


# ---------------------------------------------------------------------------
# Requirement — page three shows the function bodies being proposed: a new
# function whole, an existing one as a diff of the edit. The source travels
# on the row's symbol, so a functions page that drops `symbol` renders every
# row as a bare name and Daniel is asked to approve code he cannot see.
# ---------------------------------------------------------------------------
async def test_a_function_row_keeps_the_body_it_was_published_with(store):
    record = await store.create_design_session("Design")

    published = await store.publish_page(
        record["id"],
        "functions",
        {"f1": {"order": 1, "name": "_rotation_chain_targets", "changed": True,
                "symbol": {"name": "_rotation_chain_targets", "kind": "planned",
                           "resolved": False,
                           "source": "def _rotation_chain_targets(sessions):\n    return {}\n"},
                "note": "dropped: not a field the mind owns on this page"}},
        revision=record["revision"],
    )

    row = published["functions"]["f1"]
    assert row["symbol"]["source"] == (
        "def _rotation_chain_targets(sessions):\n    return {}\n"
    )
    assert "note" not in row
