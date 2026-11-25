"""P1/P2: rename preserves the Mind and refuses occupied destinations."""
import hashlib
import json
import uuid

import pytest
from hive_data import core, graph

pytestmark = pytest.mark.asyncio


async def test_rename_preserves_identity_credentials_conversations_and_soul(connected, inspector):
    ident = str(uuid.uuid4())
    old, new = 'old-' + ident[:8], 'new-' + ident[:8]
    await core.upsert_mind(mind_id=ident, name=old, gateway_url=f'http://{old}:8420',
                          model='model-a', harness='dsh', registered_at=12, last_seen=34,
                          session_token='session-secret', owner='daniel', voice='voice-a')
    digest = hashlib.sha256(b'unchanged-key').hexdigest()
    await core.upsert_credential(holder=old, username='daniel', role='mind',
                                 token_sha=digest, created_at=12)
    await graph.credential_put(holder=old, username='daniel', role='mind', mind_id=ident,
                               token_sha=digest, created_at=12)
    await inspector.execute(
        "INSERT INTO tools.api_tokens (name, token_hash, created_at) VALUES ($1, $2, 1)", old, digest)
    await inspector.execute("INSERT INTO core.sessions (id,mind_id,conversation_id,harness_sid,surface,surface_id,created_at,last_active) VALUES ($1,$2,$3,$4,$5,'test',1,2)",
                            ident, ident, 'conversation-kept', 'harness-kept', 'terminal')
    await graph.type_put(name='Mind', kind='first-class', property_schema={})
    await graph.node_write(owner='daniel', type='Mind', name=old, mind_id=ident,
                           properties={'soul_values': ['Keep this exact soul']})
    await core.put_setting('inference.connections', json.dumps({'connections': [], 'minds': {old: 'proxy-a'}}), 1)
    before = dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', ident))
    await core.rename_mind(ident, old, new)
    after = dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', ident))
    assert after == {**before, 'name': new, 'gateway_url': f'http://{new}:8420'}
    assert await inspector.fetchval('SELECT holder FROM core.credentials WHERE token_sha=$1', digest) == new
    assert await inspector.fetchval('SELECT holder FROM memory.credentials WHERE token_sha=$1', digest) == new
    assert await inspector.fetchval('SELECT name FROM tools.api_tokens WHERE token_hash=$1', digest) == new
    session = await inspector.fetchrow('SELECT conversation_id,harness_sid,mind_id FROM core.sessions WHERE id=$1', ident)
    assert tuple(session) == ('conversation-kept', 'harness-kept', ident)
    node = await inspector.fetchrow('SELECT name,properties FROM memory.nodes WHERE mind_id=$1 AND type=$2', ident, 'Mind')
    assert node['name'] == new
    assert json.loads(node['properties']) == {'soul_values': ['Keep this exact soul']}
    binding = json.loads(await inspector.fetchval('SELECT value FROM core.settings WHERE key=$1', 'inference.connections'))
    assert binding['minds'] == {new: 'proxy-a'}
    # Retrying a committed operation does not replace or lose anything.
    await core.rename_mind(ident, old, new)
    await core.rename_mind(ident, new, old)
    assert dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', ident)) == before


async def test_occupied_holder_refuses_rename_without_changing_identity(connected, inspector, a_mind):
    before = dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', a_mind))
    destination = 'service-' + uuid.uuid4().hex[:8]
    await core.upsert_credential(holder=destination, username='daniel', role='service', token_sha=uuid.uuid4().hex, created_at=1)
    with pytest.raises(ValueError, match='occupied'):
        await core.rename_mind(a_mind, before['name'], destination)
    assert dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', a_mind)) == before


async def test_late_provider_collision_rolls_back_registry_and_credentials(connected, inspector, a_mind):
    before = dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', a_mind))
    destination = 'collision-' + uuid.uuid4().hex[:8]
    await core.upsert_credential(holder=before['name'], username=before['owner'] or '', role='mind', token_sha=uuid.uuid4().hex, created_at=1)
    # This Mind fixture has no owner; give it one before recording its state.
    await inspector.execute('UPDATE core.minds SET owner=$2 WHERE mind_id=$1', a_mind, '')
    before['owner'] = ''
    await core.put_setting('inference.connections', json.dumps({'minds': {before['name']: 'old-provider', destination: 'occupied-provider'}}), 1)
    with pytest.raises(ValueError, match='provider binding is occupied'):
        await core.rename_mind(a_mind, before['name'], destination)
    assert dict(await inspector.fetchrow('SELECT * FROM core.minds WHERE mind_id=$1', a_mind)) == before
    assert await inspector.fetchval('SELECT holder FROM core.credentials WHERE holder=$1', before['name']) == before['name']
    assert await inspector.fetchval('SELECT holder FROM core.credentials WHERE holder=$1', destination) is None


@pytest.mark.parametrize("reserved", ["hitl-approver", "internal", "skippy"])
async def test_a_mind_cannot_be_renamed_to_a_name_the_tools_keep(connected, a_mind, reserved):
    """Its tools key would become the approver's, or the replay secret's."""
    row = await core.get_mind(a_mind)
    with pytest.raises(ValueError, match="kept for the tools"):
        await core.preflight_mind_rename(a_mind, row['name'], reserved)
    with pytest.raises(ValueError, match="kept for the tools"):
        await core.rename_mind(a_mind, row['name'], reserved)
