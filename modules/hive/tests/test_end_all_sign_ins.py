"""The two routes that end every sign-in of a person (stories 912 and 913):
a person ends their own on every device, an administrator ends anyone's.

Driven through the real hive site over HTTP; "refused" is the gateway's own
per-request check (`comms.user_access.credential_for_token`) over the same
record, so a key that still works anywhere shows here.
"""
import accounts
from comms import user_access
from multiuser_support import SEED_PASSWORD, SEED_USERNAME

import auth


def _key_of(client) -> str:
    return client.cookies.get(auth.SESSION_COOKIE_NAME)


def _gateway_knows(key: str):
    return accounts.run(user_access.credential_for_token(key))


def test_a_person_signing_out_everywhere_ends_every_device_of_theirs_and_nobody_elses(console):
    admin = console.admin()
    sloan_phone = console.settled_user(admin, "sloan", "sloan-password-1")
    sloan_laptop, signed_in = console.sign_in("sloan", "sloan-password-1")
    assert signed_in.status_code == 303
    admin_key = _key_of(admin)

    ended = sloan_phone.post("/api/sign-ins/end")

    assert ended.status_code == 200 and ended.json()["ended"] >= 2
    assert _gateway_knows(_key_of(sloan_laptop)) is None
    assert sloan_laptop.get("/overview", follow_redirects=False).status_code == 303
    assert _gateway_knows(admin_key) is not None
    assert admin.get("/overview", follow_redirects=False).status_code == 200


def test_an_administrator_ends_anyones_sign_ins_and_a_member_cannot_end_another_persons(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-password-1")
    aiden = console.settled_user(admin, "aiden", "aiden-password-1")
    sloan_key, aiden_key = _key_of(sloan), _key_of(aiden)

    refused = aiden.post("/api/admin/users/sloan/sign-ins/end")
    assert refused.status_code in (403, 303)
    assert _gateway_knows(sloan_key) is not None

    ended = admin.post("/api/admin/users/sloan/sign-ins/end")

    assert ended.status_code == 200 and ended.json()["ended"] >= 1
    assert _gateway_knows(sloan_key) is None
    assert sloan.get("/overview", follow_redirects=False).status_code == 303
    assert _gateway_knows(aiden_key) is not None
    assert _gateway_knows(_key_of(admin)) is not None
