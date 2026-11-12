"""`python3 -m installer [--home DIR]`: check, serve the setup page, install.

Prints the address, port and link (with this run's code) and writes them to
`state/installer.log`, where install.sh reads them, and to `state/page.link`
(0600). One installer per install folder: a second start while one is alive
reprints the live page's link and exits. The page stops a while after the
last run ends, finished or failed; a run started again on it (install.sh run
again after a failure) keeps it up.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import threading
import time

from installer import preflight
from installer.paths import Home, write_private

#: How long the page stays up after a run ends.
FINAL_GRACE_SECONDS = 15 * 60


def hold_page_lock(home: Home):
    """The lock an alive installer holds for its whole life, or None."""
    fh = open(home.page_lock, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


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
    lock = hold_page_lock(home)
    if lock is None:
        try:
            link = home.page_link.read_text().strip()
        except OSError:
            link = "(see the first installer's output)"
        print(f"The installer is already running here. Its page:\n  {link}")
        Shell(home.log).log(f"SETUP_URL {link}")
        return 0

    host = Host.detect()
    installer = Installer(home, host)
    server = SetupServer(installer, port=args.port)
    ended = threading.Event()
    installer.on_finished = lambda ok: ended.set()
    server.start()
    link = server.link(host.address)
    write_private(home.page_link, link + "\n")
    Shell(home.log).log(f"SETUP_URL {link}")
    print(f"Setup page: {host.address} port {server.port}\n  {link}\n"
          f"From another machine, if the port is firewalled:\n"
          f"  ssh -L {server.port}:127.0.0.1:{server.port} <this host>  then open "
          f"http://127.0.0.1:{server.port}/?code={server.code}", flush=True)
    try:
        idle_since = None
        while server.alive():
            if not ended.is_set() or installer.running():
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since > FINAL_GRACE_SECONDS:
                break
            time.sleep(1)
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - already closed by /api/close
            pass
        home.page_link.unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
