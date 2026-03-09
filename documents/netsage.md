*Project*:
Awesome—this is a perfect setup. Here’s a clean, modular plan that will work well on your gear, stay fully open-source, and keep future tweaks easy.


---

0) Wiring (quick)

TL-SG108E

Port 1: Modem

Port 2: eero WAN

Port 8 (Monitor): Ubuntu sensor’s sniffing NIC (e.g., enp3s0)


In the TL-SG108E web UI: Mirror ports 1+2 → port 8 (both directions).


Your second NIC (e.g., enp2s0) stays on your normal LAN for management / app APIs.


---

1) What to install (and where)

On the Ubuntu sensor (older 8 GB box)

Install these directly on the host (not Docker) for clean NIC access & performance:

Suricata (IDS, EVE JSON output for your app)

ipset + iptables (or UFW) for optional auto-block actions

Python 3.12 + venv for your monitoring/orchestration app

(Optional) Zeek later, if you want richer logs; start with Suricata first.


Why direct install? Suricata’s AF-PACKET / pcap capture plays nicest with the host networking stack and avoids container privilege wrangling.

On your powerful server

Ollama (you already have it). Expose it on your LAN (default 11434).

(Optional) Docker here for anything else you want to experiment with (Grafana, Loki, etc.).



---

2) Sensor host: system setup

Promiscuous NIC

# replace enp3s0 with your sniffing NIC (the one connected to TL-SG108E mirror port)
sudo ip link set enp3s0 promisc on
ip a show enp3s0 | grep PROMISC

Make persistent (systemd-networkd):

/etc/systemd/network/10-enp3s0.link
[Match]
MACAddress=xx:xx:xx:xx:xx:xx
[Link]
Promiscuous=yes

Suricata install & basic config

sudo apt update
sudo apt install -y suricata ipset iptables-persistent python3-venv jq

Enable Emerging Threats Open rules and EVE JSON:

/etc/suricata/suricata.yaml – set:

af-packet: interface → enp3s0

rule-files: include emerging.rules (via suricata-update)

outputs: enable eve-log: with types: [ alert, flow, http, dns, tls, anomaly ]



Update rules & enable service:

sudo suricata-update
sudo systemctl enable --now suricata

Logs (for your Python app) land here:

/var/log/suricata/eve.json


---

3) Project layout for your Python app

netmon/
  config.yaml
  app.py
  detectors/
    __init__.py
    intel.py           # pulls bad IP lists -> ipset + memory
    portscan.py        # custom scan heuristic (if you want beyond Suricata)
  inputs/
    tail_eve.py        # tails Suricata eve.json stream
  llm/
    ollama_client.py   # talks to your Ollama server
  actions/
    notify.py          # Discord/email/syslog
    firewall.py        # ipset/iptables block/unblock
  requirements.txt
  systemd-netmon.service

requirements.txt

pyyaml
requests
watchfiles
ujson

config.yaml (edit to taste)

suricata:
  eve_path: /var/log/suricata/eve.json

intel:
  feeds:
    - https://rules.emergingthreats.net/fwrules/emerging-Block-IPs.txt
    - https://check.torproject.org/exit-addresses

llm:
  base_url: http://OLLAMA_HOST:11434
  model: gpt-oss:20b
  max_tokens: 512
  temperature: 0.2

actions:
  notify:
    discord_webhook: ""    # or leave blank to log-only
    email: ""              # (optional) if you want email
  firewall:
    enable_autoblock: true
    block_minutes: 120

policy:
  # When to ask the LLM vs auto-block
  llm_on:
    - "suricata.alert"
  autoblock_if:
    - "ET MALWARE Known Bad IP"
    - "Portscan Detected"

inputs/tail_eve.py (tail EVE JSON and yield events)

import ujson, time

def follow_eve(path):
    with open(path, 'r') as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            try:
                yield ujson.loads(line)
            except ValueError:
                continue

detectors/intel.py (load feeds → ipset and memory)

import subprocess, requests

SET_NAME = "badnets"

def ensure_ipset():
    subprocess.run(["sudo","ipset","create",SET_NAME,"hash:ip","timeout","0"], check=False)

def load_feeds(feeds):
    ensure_ipset()
    bad = set()
    for url in feeds:
        try:
            txt = requests.get(url, timeout=10).text
            for line in txt.splitlines():
                line=line.strip()
                if not line or line.startswith("#"): continue
                ip = line.split()[0]
                bad.add(ip)
        except Exception:
            pass
    # clear and repopulate ipset
    subprocess.run(["sudo","ipset","flush",SET_NAME], check=False)
    for ip in bad:
        subprocess.run(["sudo","ipset","add",SET_NAME,ip], check=False)
    return bad

actions/firewall.py

import subprocess, time

SET_NAME = "blocklist"

def init_blocklist():
    subprocess.run(["sudo","ipset","create",SET_NAME,"hash:ip","timeout","0"], check=False)
    # Attach to INPUT/FORWARD if this box is in-path; otherwise this is for local protection.
    subprocess.run(["sudo","iptables","-C","INPUT","-m","set","--match-set",SET_NAME,"src","-j","DROP"],
                   check=False)
    subprocess.run(["sudo","iptables","-A","INPUT","-m","set","--match-set",SET_NAME,"src","-j","DROP"],
                   check=False)

def block_ip(ip, minutes=120):
    seconds = str(int(minutes)*60)
    subprocess.run(["sudo","ipset","add",SET_NAME,ip,"timeout",seconds], check=False)

llm/ollama_client.py

import requests

def analyze(base_url, model, event_summary):
    # Ollama /api/generate (simple, streaming disabled)
    payload = {"model": model, "prompt": event_summary, "stream": False}
    r = requests.post(f"{base_url}/api/generate", json=payload, timeout=60)
    r.raise_for_status()
    out = r.json().get("response","").strip()
    return out

app.py (glue)

import yaml, socket
from inputs.tail_eve import follow_eve
from detectors.intel import load_feeds
from actions.firewall import init_blocklist, block_ip
from llm.ollama_client import analyze

def summarize_eve(ev):
    sig = ev.get("alert",{}).get("signature","")
    src = ev.get("src_ip",""); spt = ev.get("src_port")
    dst = ev.get("dest_ip",""); dpt = ev.get("dest_port")
    proto = ev.get("proto","")
    return f"[Suricata] {sig} | {src}:{spt} -> {dst}:{dpt} ({proto})"

def main():
    cfg = yaml.safe_load(open("config.yaml"))
    bad_ips = load_feeds(cfg["intel"]["feeds"])
    init_blocklist()

    for ev in follow_eve(cfg["suricata"]["eve_path"]):
        if ev.get("event_type") != "alert": 
            continue

        summary = summarize_eve(ev)
        print(summary)

        # Decide: LLM analysis?
        if "suricata.alert" in cfg["policy"]["llm_on"]:
            advice = analyze(cfg["llm"]["base_url"], cfg["llm"]["model"],
                             f"Network alert:\n{summary}\n"
                             "Explain likely meaning and list 1-3 concrete remediation steps.")
            print(f"[LLM] {advice}")

        # Auto-block on certain signatures
        sig = ev.get("alert",{}).get("signature","")
        if any(key in sig for key in cfg["policy"]["autoblock_if"]):
            src = ev.get("src_ip")
            if src:
                block_ip(src, cfg["actions"]["firewall"].get("block_minutes",120))
                print(f"[ACTION] Blocked {src}")

if __name__ == "__main__":
    main()

systemd (so it runs on boot)

# netmon/systemd-netmon.service
[Unit]
Description=Home Net Monitor
After=network-online.target suricata.service
Wants=network-online.target

[Service]
WorkingDirectory=/opt/netmon
ExecStart=/opt/netmon/venv/bin/python app.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target

Install it:

sudo mkdir -p /opt/netmon
sudo rsync -av netmon/ /opt/netmon/
cd /opt/netmon
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
sudo cp systemd-netmon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now systemd-netmon


---

4) Optional: custom port-scan detector

Suricata already detects scans well via thresholds, but if you want an app-level heuristic, keep a short time-window counter per src_ip → distinct dest_port count and raise an event when it crosses your threshold (e.g., 10 ports / 10s). That event can be sent through the same LLM + actions path.


---

5) Test flow

1. Mirror check



Start tcpdump -i enp3s0 -n on the sensor.

Browse the web from any device. You should see packets.


2. Suricata



sudo tail -f /var/log/suricata/eve.json | jq '.event_type,.alert.signature?'


3. LLM link



Temporarily run:


curl -s http://OLLAMA_HOST:11434/api/generate \
  -d '{"model":"gpt-oss:20b","prompt":"Say READY","stream":false}'

Confirm your app.py logs an [LLM] line when an alert appears.


4. Auto-block



Add a test signature to autoblock_if or simulate with a dummy alert; verify ipset list blocklist.



---

6) Where to containerize (later)

Keep Suricata + the Python app on the sensor host (best signal & least friction).

Run dashboards (e.g., Grafana + Loki, or ELK/Elastic Agent) in Docker on your big server and ship eve.json → there if you want pretty graphs.



---

TL;DR

Suricata (host) to generate high-quality alerts from the mirrored link.

Python app (host) tails eve.json, consults Ollama for analysis, and triggers notify/block actions.

Ollama (big server) hosts your chosen model (gpt-oss:20b now; swap any time).

Everything is open-source, modular, and easy to extend.


If you want, I can drop in the exact suricata.yaml diffs (interface, EVE, rules) and a TL-SG108E screenshot guide for the mirror config next.


---

## Ada's Notes (2026-03-02)

### Relevant InfoSec Intelligence (TLDR InfoSec, 2026-03-02)

**ksentinel (Linux kernel module)** — Monitors syscall table integrity, function prologues, and LSTAR MSR values to detect rootkit modifications (PUMAKIT, Diamorphine, KoviD). Supports Linux 5.4–6.12+ on x86_64 and ARM64. Worth considering as a complement to NetSage: NetSage watches the *network* for anomalies, ksentinel watches the *host* for kernel-level tampering. Together they'd cover both vectors on the sensor box itself. GitHub: https://github.com/MatheuZSecurity/ksentinel

**Gardyn IoT hack** — Researchers found hardcoded admin creds and command injection on ~138,000 smart garden IoT devices, allowing unauthenticated remote compromise. This is a textbook example of what NetSage should catch: a compromised IoT device on the LAN exhibiting unusual outbound connections, unexpected DNS queries, or C2-like traffic patterns. Good validation of the project concept.

**Zero Trust / NCSC guidance** — The UK's NCSC guide emphasises continuous authentication over point-in-time trust. Relevant principle for NetSage: no device should be considered safe just because it's on the home LAN. The sensor should baseline each device's normal behaviour and flag deviations, not just match known-bad signatures.

### Architecture Observations

**Strength: Suricata + Ollama separation.** Keeping signature-based detection (Suricata) separate from LLM analysis (Ollama) is clean. Suricata handles the high-volume, low-latency work; Ollama handles the nuanced "is this actually suspicious?" reasoning on flagged events only.

**Concern: Auto-block + VPN conflict.** The doc mentions `enable_autoblock: true` with ipset/iptables, but Daniel noted that a previous firewall setup broke his Palo Alto work VPN. Since the sensor box is also the home lab's VPN gateway, auto-blocking needs to be very conservative:
- Never auto-block the VPN tunnel endpoints or Palo Alto GlobalProtect IPs
- Consider a whitelist of known-good IPs (work VPN, eero, Ollama server) that are immune from auto-block
- Default to notify-only mode until we've observed enough false-positive rates to trust auto-block
- The `firewall.py` module runs `sudo iptables` on INPUT/FORWARD — if this box isn't inline (it's passive/mirror), these rules only protect the sensor itself, not the rest of the network. This limits blast radius but also limits effectiveness.

**Concern: Running as root.** The systemd unit runs as `User=root`. This is necessary for `ipset`/`iptables` and promiscuous NIC access, but the Python app itself (LLM calls, log parsing) doesn't need root. Consider:
- Running the app as an unprivileged user with specific `sudo` permissions for just the ipset/iptables commands
- Or using Linux capabilities (`CAP_NET_RAW`, `CAP_NET_ADMIN`) instead of full root

**Opportunity: Hive Mind integration.** The current `actions/notify.py` mentions Discord webhooks and email. Since Daniel already has Hive Mind with Telegram notifications and the `notify_owner` MCP tool, NetSage could:
- Expose a simple REST API that Ada can query (e.g. `GET /api/alerts/recent`, `GET /api/stats/24h`)
- Push critical alerts to Ada via a webhook, who then notifies Daniel through the existing Telegram channel with LLM-enriched context
- Ada could periodically poll the NetSage API during the 3 AM session or via a scheduled task to check for anomalies
- Long-term: Ada could be the "brain" that correlates NetSage network data with other signals (calendar for expected absences, known device inventory, etc.)

**Opportunity: Device inventory.** Suricata's flow logs + DNS logs can build a passive device inventory over time. Every device that talks on the network leaves fingerprints (MAC address, DHCP hostname, DNS queries, TLS SNI). NetSage could maintain a `devices` table and flag when a new, unknown device appears — useful for detecting rogue devices or neighbours piggybacking on the network.

**Missing: Alerting thresholds and fatigue.** The current design sends every Suricata alert through the LLM. On an active home network, this could produce hundreds of alerts per day (especially with Emerging Threats rules, which flag a lot of benign ad/tracking traffic). Need a severity filter or aggregation layer between Suricata and the LLM to prevent alert fatigue and unnecessary Ollama load.