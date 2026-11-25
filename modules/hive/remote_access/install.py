"""Install the host controller and independent systemd user shutoffs."""
import argparse
import re
import subprocess
import sys
from pathlib import Path


def units(source: Path, state: Path, python: str, cloudflared: str, prefix="hive-remote-access", gateway_port=49310, system=False, connector_user="hive-tunnel", website_group="daniel"):
    if not re.fullmatch(r"[a-z0-9-]+", prefix):
        raise ValueError("Invalid unit prefix")
    def quoted(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    command = quoted(python) + " -m remote_access.service --state-dir " + quoted(state) + " --cloudflared " + quoted(cloudflared) + " --unit-prefix " + prefix + " --gateway-port " + str(gateway_port)
    prestart = ""
    if system:
        command += " --system --connector-user " + quoted(connector_user) + " --website-group " + quoted(website_group)
        prestart = "ExecStartPre=" + quoted(python) + " -m remote_access.egress --user " + quoted(connector_user) + " --gateway-port " + str(gateway_port) + "\n"
    if "\n" in str(source):
        raise ValueError("Invalid source path")
    base = "[Unit]\nDescription=Hive per-user remote access\n[Service]\nWorkingDirectory=" + str(source).replace("%", "%%") + "\nUMask=0077\n"
    result = {prefix + ".service": base + prestart + "ExecStart=" + command + "\nRestart=on-failure\nKillMode=control-group\n[Install]\nWantedBy=" + ("multi-user.target" if system else "default.target") + "\n",
              prefix + "-close.service": base + "Type=oneshot\nExecStart=" + command + " --off-all\n",
              prefix + "-deadline.service": base + "Type=oneshot\nExecStart=" + command + " --sweep\n"}
    for suffix, hour in (("2000", "20"), ("0200", "02")):
        result[prefix + "-" + suffix + ".timer"] = "[Unit]\nDescription=Close Hive remote access at " + hour + ":00 Central\n[Timer]\nOnCalendar=*-*-* " + hour + ":00:00 America/Chicago\nPersistent=true\nAccuracySec=1s\nUnit=" + prefix + "-close.service\n[Install]\nWantedBy=timers.target\n"
    result[prefix + "-deadline.timer"] = "[Unit]\nDescription=Close expired Hive remote connections\n[Timer]\nOnBootSec=30s\nOnUnitActiveSec=30s\nAccuracySec=1s\nUnit=" + prefix + "-deadline.service\n[Install]\nWantedBy=timers.target\n"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--cloudflared", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--unit-prefix", default="hive-remote-access")
    parser.add_argument("--gateway-port", type=int, default=49310)
    parser.add_argument("--no-enable", action="store_true")
    parser.add_argument("--system", action="store_true")
    parser.add_argument("--connector-user", default="hive-tunnel")
    parser.add_argument("--website-group", default="daniel")
    args = parser.parse_args()
    if not args.system:
        parser.error("Use --system to install the isolated host controller")
    folder = Path("/etc/systemd/system") if args.system else Path.home() / ".config/systemd/user"
    folder.mkdir(parents=True, exist_ok=True)
    for name, contents in units(Path(__file__).resolve().parents[1], args.state_dir.resolve(), args.python, args.cloudflared, args.unit_prefix, args.gateway_port, args.system, args.connector_user, args.website_group).items():
        (folder / name).write_text(contents)
    systemctl = ["systemctl"] if args.system else ["systemctl", "--user"]
    subprocess.run([*systemctl, "daemon-reload"], check=True)
    if not args.no_enable:
        subprocess.run([*systemctl, "enable", "--now", args.unit_prefix + ".service", *(args.unit_prefix + suffix for suffix in ("-2000.timer", "-0200.timer", "-deadline.timer"))], check=True)
        # Exercise the installed command, not just a copy in this shell.
        subprocess.run([*systemctl, "start", args.unit_prefix + "-close.service"], check=True)
    print("Installed per-user remote access controller and Central-time shutoffs")


if __name__ == "__main__":
    main()
