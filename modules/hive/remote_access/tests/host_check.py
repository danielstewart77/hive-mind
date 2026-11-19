"""Optional root canary for the installed ulhgpuhamster controller.

Run from the repository root: sudo ../venv/bin/python -m remote_access.tests.host_check
No real Cloudflare credential or public tunnel is used.
"""
import argparse
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from remote_access.install import units
from remote_access.manager import Manager
from remote_access.tests.test_manager import token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cloudflared', default='/usr/local/lib/hive-remote-access/cloudflared')
    parser.add_argument('--connector-user', default='hive-tunnel')
    args = parser.parse_args()
    uid = pwd.getpwnam(args.connector_user)
    root = Path(tempfile.mkdtemp(prefix='hive-remote-canary-', dir='/var/tmp'))
    root.chmod(0o755)
    prefix = 'hive-remote-access-canary'
    unit = Path('/etc/systemd/system') / (prefix + '-close.service')
    if unit.exists():
        shutil.rmtree(root)
        raise RuntimeError('Canary unit already exists; refusing to overwrite it')
    executable = root / 'connector'
    executable.write_text('#!/usr/bin/python3\n' + '''import json,sys
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
args=sys.argv
config=json.loads(Path(args[args.index('--config')+1]).read_text())
credentials=json.loads(Path(config['credentials-file']).read_text())
assert credentials['TunnelID']
host,port=args[args.index('--metrics')+1].split(':')
class Ready(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self): self.send_response(200); self.end_headers()
HTTPServer((host,int(port)),Ready).serve_forever()
''')
    executable.chmod(0o755)
    manager = Manager(root / 'state', str(executable), connector_user=args.connector_user, system=True)
    try:
        manager.configure('canary', {
            'website_url': 'https://hive.canary.example.com',
            'terminal_url': 'https://terminal.canary.example.com',
            'tunnel_token': token('00000000-0000-4000-8000-000000000099')})
        original = manager.http_get
        def external_transport(url):
            if url.startswith('https:'):
                return 302, {'location': 'https://canary.cloudflareaccess.com/login'}
            if url.endswith('/health'):
                return 200, {}
            return original(url)
        manager.http_get = external_transport
        assert manager.arm('canary')['state'] == 'armed'
        record = manager.running('canary')
        assert Path(f"/proc/{record['pid']}/status").read_text().split('Uid:')[1].split()[0] == str(uid.pw_uid)
        config = manager.user_dir('canary') / 'cloudflared.json'
        subprocess.run([args.cloudflared, 'tunnel', '--config', str(config), 'ingress', 'validate'], check=True)
        probe = "import socket; s=socket.socket(); s.settimeout(2); assert s.connect_ex(('127.0.0.1',8710)) != 0; s.close(); s=socket.socket(); s.settimeout(2); assert s.connect_ex(('127.0.0.1',49310)) == 0; print('private origin blocked; gateway reachable')"
        subprocess.run(['/usr/bin/python3', '-c', probe], user=uid.pw_uid, group=uid.pw_gid, extra_groups=[], check=True)
        generated = units(Path.cwd(), root / 'state', sys.executable, str(executable), prefix, system=True)
        unit.write_text(generated[unit.name])
        subprocess.run(['systemd-analyze', 'verify', str(unit)], check=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'start', unit.name], check=True)
        assert not manager.running('canary')
        assert (manager.user_dir('canary') / 'credentials.json').stat().st_mode & 0o777 == 0o600
        print('actual installed close unit stopped isolated connector; credentials stayed 0600')
        # Only credential/argument parsing is checked here, not a real tunnel
        # handshake. Readiness and public Access remain transport fixtures.
        manager.cloudflared = args.cloudflared
        manager.http_get = lambda url: (200, {}) if url.endswith('/ready') else external_transport(url)
        manager.arm('canary')
        time.sleep(.5)
        diagnostic = (manager.user_dir('canary') / 'connector.log').read_text()
        assert 'Starting tunnel' in diagnostic
        assert 'couldn\'t read tunnel credentials' not in diagnostic
        manager.off('canary')
        print('real Cloudflare binary read production memory credentials under the isolated UID')
    finally:
        manager.off_all()
        unit.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        shutil.rmtree(root)


if __name__ == '__main__':
    main()
