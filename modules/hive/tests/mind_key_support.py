"""A mind with its own key in the one record of keys, for the routes a mind
reaches with that key (canvas, design pages). The only thing stood in for is
the hive's own database; recognition is the real one."""
import hashlib
import time

from hive_site.testing import run


def a_mind_with_a_key(key: str, name: str = "drawer", owner: str = "daniel") -> str:
    from hive_data import core

    async def make():
        await core.upsert_mind(mind_id=f"id-{name}", name=name, gateway_url="http://x:1",
                               model="m", harness="dsh", registered_at=1.0,
                               last_seen=1.0, owner=owner)
        await core.upsert_credential(
            holder=name, username=owner, role="mind",
            token_sha=hashlib.sha256(key.encode()).hexdigest(), created_at=time.time())

    run(make())
    return name


def a_person_with_a_key(key: str, username: str = "daniel", admin: bool = True) -> str:
    """A person's own key (role user), an administrator when `admin`."""
    from hive_data import access, core

    async def make():
        now = int(time.time())
        await core.create_user(username=username, password_hash="x", created_at=now)
        user = next(u for u in await core.list_users() if str(u["username"]) == username)
        if admin:
            await core.create_group(name=access.ADMINS_GROUP, created_at=now)
            await core.add_group_member(group_name=access.ADMINS_GROUP, user_id=user["id"])
        await core.upsert_credential(
            holder=username, username=username, role=access.USER_KEY,
            token_sha=access.digest(key), created_at=time.time(), revoked_at=None)

    run(make())
    return username
