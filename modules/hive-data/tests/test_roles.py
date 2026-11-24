"""What an agent may use: the union of the roles it holds (feature 10.18).

Protected behaviours 1 and 2 of Role Management, at the one place both the
console and the gateway read them from: an agent holding several roles may
use exactly the union of what they grant and nothing else, and changing a
role changes what every agent holding it may use on the very next read.

The scratch database is shared by the whole run, so every role here carries a
name of its own; the general role is the one row every test shares, and each
test sets it to what that test needs.
"""

from __future__ import annotations

import uuid

from hive_data import roles


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _general(**grants) -> int:
    row = await roles.role_by_slug(roles.GENERAL)
    if row is None:
        made = await roles.create_role("General")
        row = {"id": made["row"]["id"]}
    await roles.update_role(row["id"], **{"skills": [], "hooks": [], "model": None,
                                          "tools": [], **grants})
    return row["id"]


async def _role(label: str, **grants) -> int:
    made = await roles.create_role(f"{label} {_tag()}", **grants)
    return made["row"]["id"]


async def test_an_agent_holding_two_roles_may_use_their_union_and_nothing_else(connected):
    await _general(skills=["memory"], hooks=["time_inject"])
    writer = await _role("Writer", skills=["remember", "pdf-formatter"],
                         tools=["files"], model="claude-haiku-5-5")
    builder = await _role("Builder", skills=["code", "remember"],
                          hooks=["training_capture"], tools=["shell", "web"],
                          model="claude-opus-5-5")
    await _role("Unheld", skills=["send-sms"], tools=["sub-minds"])
    mind = f"mind-{_tag()}"

    await roles.set_mind_roles(mind, [writer, builder])
    grants = await roles.grants_for(mind)

    assert grants["managed"] is True
    assert grants["skills"] == ["code", "memory", "pdf-formatter", "remember"]
    assert grants["hooks"] == ["time_inject", "training_capture"]
    assert sorted(grants["models"]) == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert grants["tools"] == ["shell", "files", "web"]


async def test_taking_one_role_away_keeps_what_the_other_still_grants(connected):
    await _general()
    writer = await _role("Writer", skills=["remember", "pdf-formatter"], tools=["files"])
    builder = await _role("Builder", skills=["code", "remember"], tools=["shell"])
    mind = f"mind-{_tag()}"
    await roles.set_mind_roles(mind, [writer, builder])

    await roles.set_mind_roles(mind, [builder])
    grants = await roles.grants_for(mind)

    assert grants["skills"] == ["code", "remember"]
    assert grants["tools"] == ["shell"]


async def test_changing_a_role_changes_every_agent_holding_it_on_the_next_read(connected):
    await _general()
    shared = await _role("Shared", skills=["remember"], tools=["files"])
    first, second = f"mind-{_tag()}", f"mind-{_tag()}"
    await roles.set_mind_roles(first, [shared])
    await roles.set_mind_roles(second, [shared])

    await roles.update_role(shared, skills=["remember", "code"], tools=["files", "web"])

    for mind in (first, second):
        grants = await roles.grants_for(mind)
        assert grants["skills"] == ["code", "remember"]
        assert grants["tools"] == ["files", "web"]

    await roles.update_role(shared, skills=["remember"], tools=[])
    for mind in (first, second):
        grants = await roles.grants_for(mind)
        assert grants["skills"] == ["remember"]
        assert grants["tools"] == []


async def test_a_change_to_the_general_role_reaches_every_managed_agent(connected):
    general = await _general(skills=["memory"])
    first, second = f"mind-{_tag()}", f"mind-{_tag()}"
    await roles.set_mind_roles(first, [])
    await roles.set_mind_roles(second, [await _role("Other", skills=["code"])])

    await roles.update_role(general, skills=["memory", "rotate-session"])

    assert (await roles.grants_for(first))["skills"] == ["memory", "rotate-session"]
    assert (await roles.grants_for(second))["skills"] == ["code", "memory", "rotate-session"]


async def test_deleting_the_only_role_an_agent_held_leaves_it_holding_general_alone(connected):
    await _general(skills=["memory"])
    only = await _role("Only", skills=["code"], tools=["shell", "files", "web"])
    mind = f"mind-{_tag()}"
    await roles.set_mind_roles(mind, [only])

    await roles.delete_role(only)
    grants = await roles.grants_for(mind)

    assert grants["managed"] is True
    assert grants["roles"] == [roles.GENERAL]
    assert grants["skills"] == ["memory"]
    assert grants["tools"] == []


async def test_the_general_role_cannot_be_deleted(connected):
    general = await _general()

    result = await roles.delete_role(general)

    assert result["rows"] == 0
    assert await roles.get_role(general) is not None


async def test_an_agent_never_given_roles_is_not_under_role_management(connected):
    await _general(skills=["memory"])

    grants = await roles.grants_for(f"mind-{_tag()}")

    assert grants["managed"] is False
    assert grants["roles"] == []


async def test_two_roles_cannot_share_a_name(connected):
    import hive_data
    import pytest

    name = f"Planner {_tag()}"
    await roles.create_role(name)
    with pytest.raises(hive_data.Conflict):
        await roles.create_role(name.upper())


async def test_renaming_a_role_keeps_its_slug(connected):
    made = await roles.create_role(f"Chief of Staff {_tag()}")
    role_id = made["row"]["id"]
    before = (await roles.get_role(role_id))["slug"]

    await roles.update_role(role_id, name="Atlas's job")

    after = await roles.get_role(role_id)
    assert after["name"] == "Atlas's job"
    assert after["slug"] == before


async def test_a_role_cannot_be_renamed_to_another_roles_name(connected):
    import hive_data
    import pytest

    taken = f"Developer {_tag()}"
    await roles.create_role(taken)
    other = (await roles.create_role(f"Other {_tag()}"))["row"]["id"]

    with pytest.raises(hive_data.Conflict):
        await roles.update_role(other, name=f"  {taken.lower()} ")
