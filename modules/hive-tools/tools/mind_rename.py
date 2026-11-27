"""One approved, recoverable host transition; no whole-stack Docker commands."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import yaml
from hive_data import core

from auth_api import require_api_token_or_person as require_api_token
from tools.docker_ops import docker_mutate_gate

router = APIRouter(prefix='/docker/mind-renames', tags=['docker'])


def _name(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,31}', value):
        raise ValueError('Invalid Mind name')
    return value


def _directory(root: Path, operation_id: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', operation_id):
        raise ValueError('Invalid operation ID')
    result = root / 'updates' / 'mind-renames' / operation_id
    if not result.resolve().is_relative_to(root.resolve()) or result.is_symlink():
        raise ValueError('Invalid operation directory')
    return result


def save(root, operation):
    directory = _directory(root, operation['operation_id'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'operation.json'
    temporary = directory / 'operation.tmp'
    with temporary.open('w') as stream:
        json.dump(operation, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load(root, operation_id):
    return json.loads((_directory(root, operation_id) / 'operation.json').read_text())


def _variables(old, new):
    """Every variable a mind's compose names by the mind: its one key, the
    bot's own credential, the older per-service spellings and the
    `<NAME>_PROXY_KEY` one some hand-kept minds use."""
    stem_old, stem_new = old.replace('-', '_').upper(), new.replace('-', '_').upper()
    names = {f'{prefix}_TOKEN_{stem_old}': f'{prefix}_TOKEN_{stem_new}'
             for prefix in ('COMMS', 'LUCENT', 'PROXY', 'TOOLS')}
    names[f'MIND_KEY_{stem_old}'] = f'MIND_KEY_{stem_new}'
    names[f'PROACTIVE_TOKEN_{stem_old}'] = f'PROACTIVE_TOKEN_{stem_new}'
    if stem_old not in ('MIND', 'SERVICE'):  # those spellings are the install's own keys
        names[f'{stem_old}_PROXY_KEY'] = f'{stem_new}_PROXY_KEY'
    return names


def reserve(root, operation_id, mind_id, old, new, caller):
    root = Path(root)
    _name(old), _name(new)
    directory = _directory(root, operation_id)
    if (directory / 'operation.json').exists():
        existing = load(root, operation_id)
        if any(existing[key] != value for key, value in
               (('old', old), ('new', new), ('mind_id', mind_id), ('caller', caller))):
            raise ValueError('This operation belongs to another rename')
        return existing
    source, target = root / 'minds' / old, root / 'minds' / new
    if source.is_symlink() or not source.is_dir() or not source.resolve().is_relative_to(root.resolve()):
        raise ValueError('This Mind has no supported local runtime')
    if os.path.lexists(target):
        raise ValueError('The destination folder already exists')
    for relative in ('runtime.yaml', 'container/compose.yaml'):
        if not (source / relative).resolve().is_relative_to(source.resolve()):
            raise ValueError('The runtime configuration leaves this Mind’s folder')
    runtime = yaml.safe_load((source / 'runtime.yaml').read_text())
    if runtime.get('mind_id') != mind_id or runtime.get('name') != old:
        raise ValueError('The local runtime does not match this Mind')
    document = yaml.safe_load((source / 'container' / 'compose.yaml').read_text())
    if set(document.get('services', {})) != {old}:
        raise ValueError('This Mind does not have its own Compose fragment')
    env = (root / '.env').read_text()
    for key in _variables(old, new).values():
        if re.search(r'^\s*(?:export\s+)?' + re.escape(key) + r'\s*=', env, re.M):
            raise ValueError('The destination credential variable is occupied')
    stack = (root / 'docker-compose.yml').read_text()
    if f'minds/{old}/container/compose.yaml' not in stack:
        raise ValueError('This Mind is not included in this host’s stack')
    for path in (root / 'updates' / 'mind-renames').glob('*/operation.json'):
        existing = json.loads(path.read_text())
        if existing.get('stage') not in ('committed', 'rolled_back', 'cancelled') and {old, new} & {existing['old'], existing['new']}:
            raise ValueError('A pending rename already reserves this name')
    operation = {'operation_id': operation_id, 'mind_id': mind_id, 'old': old,
                 'new': new, 'caller': caller, 'stage': 'reserved'}
    save(root, operation)
    return operation


@contextlib.contextmanager
def locked(root, operation_id):
    directory = _directory(root, operation_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'lock').open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _patch_environment(root, old, new):
    path = root / '.env'
    mapping = _variables(old, new)
    text = path.read_text()
    for source, target in mapping.items():
        text = re.sub(r'^(\s*(?:export\s+)?)' + re.escape(source) + r'(\s*=)',
                      lambda match: match[1] + target + match[2], text, flags=re.M)
    # This file is individually bind-mounted into Hive. Keep its inode.
    with path.open('r+') as stream:
        stream.write(text)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def _patch_stack(root, old, new):
    path = root / 'docker-compose.yml'
    text = path.read_text().replace(f'minds/{old}/container/compose.yaml', f'minds/{new}/container/compose.yaml')
    with path.open('r+') as stream:
        stream.write(text)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def _write_compose(root, name, content):
    """Keep root-owned deployment files protected from the Mind itself.

    A scaffold's Compose directory is root-owned. Tools' app user must not
    chown it to the Mind's UID to edit it. A short-lived helper uses this
    Tools image, no network/socket, and one exact file mount. The content
    goes over stdin, never command-line arguments or container metadata.
    """
    path = root / 'minds' / _name(name) / 'container' / 'compose.yaml'
    if not path.resolve().is_relative_to((root / 'minds' / name).resolve()):
        raise ValueError('The deployment file leaves this Mind’s folder')
    try:
        path.write_text(content)
    except PermissionError:
        image = os.environ.get('HIVE_RENAME_HELPER_IMAGE') or json.loads(_docker(root, ['inspect', 'hive-tools']))[0]['Image']
        script = 'import sys; p="/target"; f=open(p,"r+"); f.write(sys.stdin.read()); f.truncate(); f.flush(); import os; os.fsync(f.fileno()); f.close()'
        result = subprocess.run(['docker', 'run', '--rm', '-i', '--user', '0', '--network', 'none',
                                 '--entrypoint', '/opt/venv/bin/python', '--mount', f'type=bind,source={path},target=/target',
                                 image, '-c', script], cwd=root, input=content, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise ValueError('The protected deployment file could not be updated')


KEY_MOUNT = '/run/mind-key'


def _key_folder(root, name):
    """This mind's key folder on the host (requirement 21), when the install's
    `.env` names a key root that exists here: `<HOST_KEY_DIR>/<name>`. None
    otherwise (a mind with no key folder, or a key root this service cannot
    see; then the operator moves the folder with the rename)."""
    try:
        text = (root / '.env').read_text()
    except OSError:
        return None
    found = re.findall(r"^\s*(?:export\s+)?HOST_KEY_DIR\s*=\s*['\"]?([^'\"\n]+)['\"]?\s*$", text, flags=re.M)
    if not found:
        return None
    folder = Path(found[-1].strip()) / name
    return folder if folder.parent.is_dir() else None


def _move_key_folder(root, old, new):
    """Rename a mind's key folder with the mind: its compose mounts the folder
    by the mind's name, and the console looks it up by name."""
    source, target = _key_folder(root, old), _key_folder(root, new)
    if source is None or target is None or not source.is_dir():
        return
    if os.path.lexists(target):
        raise ValueError('The destination key folder already exists')
    source.rename(target)


def rename_files(root, operation):
    """Called only with a stopped Mind. User-authored files are moved intact."""
    old, new = operation['old'], operation['new']
    source, target = root / 'minds' / old, root / 'minds' / new
    directory = _directory(root, operation['operation_id'])
    for relative, backup in (('runtime.yaml', 'runtime.backup'), ('container/compose.yaml', 'compose.backup')):
        if not (directory / backup).exists():
            shutil.copy2(source / relative, directory / backup)
            os.chmod(directory / backup, 0o600)
    operation['stage'] = 'moving'
    save(root, operation)
    if source.exists():
        if os.path.lexists(target):
            raise ValueError('The destination folder already exists')
        source.rename(target)
    _move_key_folder(root, old, new)
    runtime = yaml.safe_load((directory / 'runtime.backup').read_text())
    runtime.update(name=new, gateway_url=f'http://{new}:8420')
    (target / 'runtime.yaml').write_text(yaml.safe_dump(runtime, sort_keys=False))
    document = yaml.safe_load((directory / 'compose.backup').read_text())
    service = document['services'].pop(old)
    document['services'][new] = service
    service['container_name'] = 'hive-mind-' + new
    labels = service.get('labels') or {}
    labels = dict(item.split('=', 1) for item in labels if '=' in item) if isinstance(labels, list) else dict(labels)
    labels['hive.rename.operation'] = operation['operation_id']
    service['labels'] = labels
    env = service.get('environment', [])
    as_list = isinstance(env, list)
    env = dict(item.split('=', 1) for item in env if '=' in item) if as_list else dict(env)
    env['MIND_NAME'] = new
    for key, value in env.items():
        for before, after in _variables(old, new).items():
            value = str(value).replace('${' + before + '}', '${' + after + '}')
        env[key] = value
    service['environment'] = [f'{key}={value}' for key, value in env.items()] if as_list else env
    volumes = []
    for volume in service.get('volumes', []):
        if isinstance(volume, str):
            source_mount, separator, destination = volume.partition(':/')
            source_mount = re.sub(r'/minds/' + re.escape(old) + r'(?=/|$)', '/minds/' + new, source_mount)
            if separator and destination.startswith(KEY_MOUNT[1:]):
                # The mind's key folder, mounted by the mind's name.
                source_mount = re.sub(r'/' + re.escape(old) + r'$', '/' + new, source_mount)
            migrated = source_mount + separator + destination
            volumes.append(migrated)
            # Both internal paths point at the same persisted files. Historic
            # CWD/harness transcripts still resolve; new runtime loads by name.
            if separator and re.match(r'usr/src/app/minds/' + re.escape(old) + r'(?=/|:|$)', destination):
                volumes.append(source_mount + separator + re.sub(r'^usr/src/app/minds/' + re.escape(old), 'usr/src/app/minds/' + new, destination))
        elif isinstance(volume, dict):
            migrated = dict(volume)
            migrated['source'] = re.sub(r'/minds/' + re.escape(old) + r'(?=/|$)', '/minds/' + new, str(volume.get('source', '')))
            if str(volume.get('target', '')) == KEY_MOUNT:
                migrated['source'] = re.sub(r'/' + re.escape(old) + r'$', '/' + new, migrated['source'])
            volumes.append(migrated)
            destination = str(volume.get('target', ''))
            if destination.startswith('/usr/src/app/minds/' + old):
                volumes.append({**migrated, 'target': destination.replace('/usr/src/app/minds/' + old, '/usr/src/app/minds/' + new, 1)})
    service['volumes'] = volumes
    networks = service.get('networks', {})
    networks = {name: {} for name in networks} if isinstance(networks, list) else dict(networks)
    for name, options in networks.items():
        options = dict(options or {})
        options['aliases'] = [alias for alias in options.get('aliases', []) if alias != old]
        networks[name] = options
    service['networks'] = networks
    command = service.get('command')
    if isinstance(command, list):
        service['command'] = [item.replace('--mind ' + old + ' ', '--mind ' + new + ' ') if isinstance(item, str) else item for item in command]
    _write_compose(root, new, yaml.safe_dump(document, sort_keys=False))
    _patch_environment(root, old, new)
    _patch_stack(root, old, new)
    operation['stage'] = 'moved'
    save(root, operation)


def rollback_files(root, operation):
    old, new = operation['old'], operation['new']
    source, target = root / 'minds' / old, root / 'minds' / new
    directory = _directory(root, operation['operation_id'])
    if target.exists():
        if source.exists():
            raise ValueError('Both folders exist; recovery needs inspection')
        target.rename(source)
    for relative, backup in (('runtime.yaml', 'runtime.backup'), ('container/compose.yaml', 'compose.backup')):
        if (directory / backup).exists():
            # Write in place so runtime ownership/mode are preserved.
            if relative == 'container/compose.yaml':
                _write_compose(root, old, (directory / backup).read_text())
            else:
                (source / relative).write_bytes((directory / backup).read_bytes())
    _patch_environment(root, new, old)
    _patch_stack(root, new, old)
    try:
        _move_key_folder(root, new, old)
    except (OSError, ValueError):
        pass


def _docker(root, arguments):
    result = subprocess.run(['docker', *arguments], cwd=root, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Docker output may contain resolved environment. Never relay it.
        raise ValueError('The Mind’s container operation failed; recovery is required')
    return result.stdout.strip()


def _compose(root, name):
    return ['compose', '-f', str(root / 'docker-compose.yml'), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', name]


def transition(root, operation):
    old, new = operation['old'], operation['new']
    if operation['stage'] == 'running':
        return operation
    if operation['stage'] != 'authorized':
        raise ValueError('This rename must be authorized before it can run')
    names = _docker(root, ['ps', '-a', '--format', '{{.Names}}']).splitlines()
    if 'hive-mind-' + new in names:
        raise ValueError('The destination container already exists')
    # Check the actual container and retain the exact image, not a mutable tag.
    original = json.loads(_docker(root, ['inspect', 'hive-mind-' + old]))[0]
    if original['Config'].get('Labels', {}).get('com.docker.compose.service') != old:
        raise ValueError('The running container does not belong to this Mind')
    operation['image_id'] = original['Image']
    operation['was_running'] = bool(original['State']['Running'])
    if not operation['was_running']:
        raise ValueError('Start this Mind before requesting its rename')
    operation['stage'] = 'stopping'
    save(root, operation)
    try:
        _docker(root, ['stop', 'hive-mind-' + old])
        operation['stage'] = 'stopped'
        save(root, operation)
        rename_files(root, operation)
        # Bind the renamed service to the exact running image. Its build
        # configuration is retained for subsequent intentional upgrades.
        compose_path = root / 'minds' / new / 'container' / 'compose.yaml'
        document = yaml.safe_load(compose_path.read_text())
        document['services'][new]['image'] = operation['image_id']
        _write_compose(root, new, yaml.safe_dump(document, sort_keys=False))
        _docker(root, ['compose', '-f', str(root / 'docker-compose.yml'), 'config', '--quiet'])
        _docker(root, _compose(root, new))
        operation['stage'] = 'running'
        save(root, operation)
        return operation
    except BaseException:
        rollback(root, operation)
        raise


def rollback(root, operation):
    if operation['stage'] in ('reserved', 'authorized', 'cancelled'):
        operation['stage'] = 'cancelled'
        save(root, operation)
        return operation
    operation['stage'] = 'recovering'
    save(root, operation)
    # rm -f targets only the new container and keeps every persistent mount.
    inspected = subprocess.run(['docker', 'inspect', 'hive-mind-' + operation['new']], cwd=root, capture_output=True, text=True, timeout=120)
    if inspected.returncode == 0:
        container = json.loads(inspected.stdout)[0]
        if container['Config'].get('Labels', {}).get('hive.rename.operation') != operation['operation_id']:
            raise ValueError('The destination container belongs to another operation')
        _docker(root, ['rm', '-f', 'hive-mind-' + operation['new']])
    rollback_files(root, operation)
    if operation.get('was_running'):
        # Keep the original stopped container until commit. Recovery starts
        # its exact image/configuration, even if a mutable tag changed.
        _docker(root, ['start', 'hive-mind-' + operation['old']])
    operation['stage'] = 'rolled_back'
    save(root, operation)
    return operation


def project(path):
    configured = os.environ.get('HIVE_RENAME_PROJECT_DIR', '')
    root = Path(path).resolve()
    if not configured or root != Path(configured).resolve():
        raise HTTPException(400, 'Mind rename is not configured for this host path')
    return root


def owned(root, operation_id, caller):
    try:
        operation = load(root, operation_id)
    except (OSError, ValueError):
        raise HTTPException(404, 'Rename not found') from None
    if operation['caller'] != caller:
        raise HTTPException(404, 'Rename not found')
    return operation


@router.post('/reserve')
async def reserve_route(request: Request, path: str = Query(...), caller=Depends(require_api_token)):
    body = await request.json()
    root = project(path)
    try:
        with locked(root, 'reservations'):
            return reserve(root, str(body['operation_id']), str(body['mind_id']), str(body['old']), str(body['new']), caller)
    except (ValueError, KeyError, OSError):
        raise HTTPException(409, 'The destination is unavailable or the local runtime cannot be renamed') from None


@router.post('/{operation_id}/authorize')
async def authorize_route(operation_id: str, path: str = Query(...), caller=Depends(docker_mutate_gate('docker_mind_rename'))):
    root = project(path)
    with locked(root, operation_id):
        operation = owned(root, operation_id, caller)
        if operation['stage'] == 'reserved':
            operation['stage'] = 'authorized'
            save(root, operation)
        if operation['stage'] == 'cancelled':
            raise HTTPException(409, 'This rename was cancelled')
        return operation


@router.get('/{operation_id}')
async def status_route(operation_id: str, path: str = Query(...), caller=Depends(require_api_token)):
    return owned(project(path), operation_id, caller)


@router.post('/{operation_id}/{action}')
async def action_route(operation_id: str, action: str, path: str = Query(...), caller=Depends(require_api_token)):
    root = project(path)
    pending = owned(root, operation_id, caller)
    if action == 'continue':
        gate = await core.get_setting('mind.rename.' + pending['mind_id'])
        if not gate or json.loads(gate['value'] or '{}').get('operation_id') != operation_id:
            raise HTTPException(409, 'This Mind has not reached its safe rename boundary')
    if action == 'confirm':
        mind = await core.get_mind(pending['mind_id'])
        if not mind or mind['name'] != pending['new']:
            raise HTTPException(409, 'The registry has not committed this rename')
    def run():
        with locked(root, operation_id):
            operation = owned(root, operation_id, caller)
            if action == 'continue':
                return transition(root, operation)
            if action == 'rollback':
                if operation['stage'] == 'committed':
                    raise ValueError('A committed rename cannot be rolled back by this operation')
                return rollback(root, operation)
            if action == 'confirm' and operation['stage'] in ('running', 'committed'):
                operation['stage'] = 'committed'
                save(root, operation)
                subprocess.run(['docker', 'rm', 'hive-mind-' + operation['old']], cwd=root, capture_output=True, timeout=120)
                return operation
            raise ValueError('Invalid rename transition')
    try:
        return await asyncio.to_thread(run)
    except (ValueError, OSError):
        raise HTTPException(409, 'The rename could not complete. Its recovery status is available') from None
