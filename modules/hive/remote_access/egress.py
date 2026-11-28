"""Host egress fence for the dedicated connector UID, including remote ingress.

Cloudflare account settings can replace local ingress. They must never give
the connector a path directly to Hive's private services or another LAN host.
"""
import argparse
import ipaddress
import pwd
import subprocess


def install(user, gateway_port=49310):
    uid = pwd.getpwnam(user).pw_uid
    if uid == 0:
        raise ValueError("Refusing to restrict the root account")
    for binary, networks in (("iptables", ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4"]),
                             ("ip6tables", ["::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"])):
        def command(*args, check=True):
            return subprocess.run([binary, "-w", *args], check=check, capture_output=True)
        chain = "HIVE-TUNNEL"
        # Install before any connector starts. This UID is used for nothing else.
        command("-N", chain, check=False)
        command("-F", chain)
        command("-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
        if binary == "iptables":
            command("-A", chain, "-d", "127.0.0.1", "-p", "tcp", "--dport", str(gateway_port), "-j", "ACCEPT")
        # Permit only the host's actual DNS resolvers on port 53.
        from pathlib import Path
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == "nameserver":
                address = ipaddress.ip_address(fields[1])
                if (address.version == 4) == (binary == "iptables"):
                    for protocol in ("udp", "tcp"):
                        command("-A", chain, "-d", str(address), "-p", protocol, "--dport", "53", "-j", "ACCEPT")
        for network in networks:
            command("-A", chain, "-d", network, "-j", "REJECT")
        command("-A", chain, "-j", "RETURN")
        rule = ["OUTPUT", "-m", "owner", "--uid-owner", str(uid), "-j", chain]
        if command("-C", *rule, check=False).returncode:
            command("-I", *rule)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--gateway-port", type=int, default=49310)
    args = parser.parse_args()
    install(args.user, args.gateway_port)
