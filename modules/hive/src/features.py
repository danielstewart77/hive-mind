"""Navigation preferences, stored per account in the Hive database."""
import json

from fastapi import HTTPException

import store

# Stable IDs, shared by the settings list and the sidebar.
FEATURES = [
    ('overview', 'Overview', False), ('minds', 'Minds', False),
    ('usage', 'Usage', False), ('roles', 'Roles', True),
    ('bots', 'Bots', True), ('activity', 'Activity', True),
    ('dashboard', 'Dashboard', True), ('hive-errors', 'Hive Errors', True),
    ('edge-minds', 'Edge Minds', True), ('capability-nodes', 'Capability Nodes', True),
    ('graph', 'Graph', True), ('memory', 'Memory', True), ('rules', 'Rules', True),
    ('sentinel', 'Sentinel', True), ('training', 'Training', True),
    ('harness-runs', 'Harness runs', True), ('chief-of-staff', 'Chief of Staff', True),
    ('kanban', 'Kanban', True), ('kanban-backlog', 'Kanban · Backlog', True),
    ('kanban-board', 'Kanban · Board', True), ('canvas', 'Canvas', True), ('design', 'Design', True),
    ('terminal', 'Terminal', False), ('setup', 'Setup guide', True),
]


async def enabled(user):
    saved = await store.get_navigation_features(user['username'])
    return {key: saved.get(key, True) for key, _, _ in FEATURES}


async def save(user, choices):
    if not isinstance(choices, dict) or not choices:
        raise HTTPException(422, 'Supply feature names and checkbox values')
    known = {key for key, _, _ in FEATURES}
    if any(key not in known or type(value) is not bool for key, value in choices.items()):
        raise HTTPException(422, 'Unknown feature or invalid checkbox value')
    # Keep the saved preference set in one database transaction.
    async with store.transaction(hold='navigation.features:' + user['username']):
        current = await enabled(user)
        current.update(choices)
        await store.put_navigation_features(user['username'], json.dumps(current))
    return current
