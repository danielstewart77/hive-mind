"""`python3 -m installer [--home DIR]`: check, serve the setup page, install.

Prints the address, port and link (with this run's code) and writes them to
`state/installer.log`, where install.sh reads them. The page stops a while
after the install finishes.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from installer import preflight
from installer.paths import Home

#: How long the final page stays up after a finished install.
FINAL_GRACE_SECONDS = 15 * 60


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m installer")
    parser.add_argument("--home", help="install folder (default $HIVE_HOME or ~/hive)")
    parser.add_argument("--port", type=int, default=0, help="page port (default: a free one)")
    args = parser.parse_args(argv)
    home = Home.at(args.home)

    found = preflight.problems(home.root)
    if found:
        for line in found:
            print(f"hive install: {line}", file=sys.stderr)
        return 1

    from installer.app import Installer
    from installer.host import Host
    from installer.server import SetupServer
    from installer.shell import Shell

    home.ensure()
    host = Host.detect()
    installer = Installer(home, host)
    server = SetupServer(installer, port=args.port)
    done = threading.Event()
    installer.on_finished = done.set
    server.start()
    link = server.link(host.address)
    Shell(home.log).log(f"SETUP_URL {link}")
    print(f"Setup page: {host.address} port {server.port}\n  {link}\n"
          f"From another machine, if the port is firewalled:\n"
          f"  ssh -L {server.port}:127.0.0.1:{server.port} <this host>  then open "
          f"http://127.0.0.1:{server.port}/?code={server.code}", flush=True)
    try:
        while not done.is_set():
            if not server._thread.is_alive():
                return 0
            time.sleep(1)
        deadline = time.monotonic() + FINAL_GRACE_SECONDS
        while time.monotonic() < deadline and server._thread.is_alive():
            time.sleep(2)
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - already closed by /api/close
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
