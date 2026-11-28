# Personal remote access

Each Hive account can configure and arm its own Cloudflare tunnel in
**Settings → Remote access**, using a tunnel in that person's Cloudflare
account. The navigation shortcut arms or disarms the saved connection.
Members have the same personal controls as admins. An admin can disable
remote access globally; this closes every connector and prevents rearming.

## Configure your Cloudflare account

1. Create a dedicated Cloudflare Tunnel for your Hive account. Do not reuse
   a tunnel that serves other applications: disarming stops this connector.
2. Give it two public hostnames, one for the Hive website and one for the
   web terminal. Set both origin services to `http://127.0.0.1:49310`.
   This is the host gateway, which forwards each hostname to its service.
3. Create Cloudflare Access applications covering **both entire hostnames**,
   with a sign-in policy permitting your identity. Enable WebSockets for
   the web terminal. An unprotected address prevents arming and rolls back
   the connector.
4. Copy the tunnel's connector token (the encoded token from Cloudflare's
   connector install command), and paste it with the two HTTPS addresses
   into Hive's Remote access form. This is not an account API key or a Mind
   inference key. The saved token is never returned by the website.
5. Save and arm while on the local network. Sign into Cloudflare Access,
   then into Hive as the account that owns the tunnel. The website and web
   terminal use host-only cookies, so each address can require its own login.

The connection closes at the next **20:00 or 02:00 America/Chicago** boundary.
Host timers enforce both boundaries even if the website is stopped; a
30-second sweep also catches expiration. The gateway closes open remote
WebSockets when the connection expires or is disarmed. Minds and their
tmux conversations keep running. Restarting the controller leaves tunnels
disarmed. Remote rearming while a tunnel is down requires local website
access; no out-of-band arm command is included.

Cloudflare account configuration may replace local ingress. The host's
dedicated connector UID therefore has an IPv4/IPv6 egress fence: it may
reach the account-checking gateway, configured DNS resolvers, and public
Cloudflare services, but not private service ports or LAN destinations.
The gateway requires the hostname owner's real Hive session for protected
HTTP and all WebSockets. Admin status does not bypass ownership.

## Linux host installation

The website's existing `/data` volume must share the controller directory.
On ulhgpuhamster this is `/home/daniel/hive/data/console/remote-access` on the
host and `/data/remote-access` in the website. The website needs access to
the private control socket and verifier; it does not receive connector tokens.

Install a checksum-verified official `cloudflared` executable in a root-owned,
world-executable path (on this host `/usr/local/lib/hive-remote-access/cloudflared`), a Python 3.12
virtual environment with `remote_access/requirements.txt`, and a dedicated
system account with no home or login shell:

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin hive-tunnel
python3 -m venv /path/to/venv
/path/to/venv/bin/pip install -r remote_access/requirements.txt
sudo /path/to/venv/bin/python -m remote_access.install --system \
  --state-dir /home/daniel/hive/data/console/remote-access \
  --cloudflared /path/to/cloudflared --website-group daniel
```

Run from the checked-out repository root. The root controller drops each
connector to `hive-tunnel`, passes its credential through memory descriptors,
and keeps stored credentials mode 0600. It installs only UID-specific
`iptables`/`ip6tables` OUTPUT rules, under `HIVE-TUNNEL`; it does not change
Mind containers or their networking. Do not assign this UID to other software.
The controller installs the rules before starting connectors on each boot.
Root-only code and virtual-environment ownership is recommended for a shared
host; keep installation paths under the host administrator's control.

Verify using `systemctl is-active hive-remote-access.service
hive-remote-access-2000.timer hive-remote-access-0200.timer
hive-remote-access-deadline.timer`, then `systemctl list-timers
'hive-remote-access*'`. The installer exercises the actual close service.

For independent process/network verification on ulhgpuhamster, from the
repository root run `sudo ../venv/bin/python -m remote_access.tests.host_check`.
This creates a temporary connector under the dedicated UID, validates real
Cloudflare ingress, proves private origin access is blocked, runs a generated
systemd close unit, and removes that canary unit. It never arms a public tunnel.

## Verification and recovery

Automated protections: `python -m pytest remote_access/tests -q` and the
website's `tests/test_remote_access_routes.py` suite, with a scratch database.
Gateway tests use real HTTP/WebSocket forwarding and actual process shutdown;
Cloudflare transport is a fixture. A successful external arm still requires
your real Cloudflare token, DNS routes, and Access policies.

To close every connector independently of the website:
`sudo systemctl start hive-remote-access-close.service`.
To stop the controller: `sudo systemctl stop hive-remote-access.service`.
Stopping it terminates connector children; it never stops Minds.
Website rollback on this host uses image `hive/site:configured-providers`
and the saved Compose file `docker-compose.before-remote-access.yml`.
