---
name: setup-prerequisites
description: Detect hardware, OS, and software prerequisites for Hive Mind deployment. Checks Docker, Git, RAM, GPU, CPU architecture, and disk space. Use when starting a fresh deployment or verifying system readiness.
user-invocable: true
tools: Bash
---

# setup-prerequisites

Detect hardware, OS, and software prerequisites for Hive Mind deployment. Must run before anything else.

## Step 1 — OS Detection

Detect the operating system and distribution:

```bash
uname -s
```

- **Linux**: Read `/etc/os-release` for distribution details (`cat /etc/os-release`).
  - Check if running under WSL: `grep -qi microsoft /proc/version 2>/dev/null && echo "WSL detected"`.
- **Darwin**: macOS. Note the version: `sw_vers`.
- **Other**: Warn that the platform is untested.

## Step 2 — CPU Architecture

```bash
uname -m
```

Record the architecture:
- `x86_64` / `amd64` — standard Intel/AMD
- `aarch64` / `arm64` — ARM (Apple Silicon, Raspberry Pi, ARM servers)
- Other — warn that Docker images may not be available

## Step 3 — RAM

**Linux/WSL:**
```bash
free -h | head -2
```
Extract total RAM. Parse the numeric value.

**macOS:**
```bash
sysctl -n hw.memsize | awk '{print $1/1024/1024/1024 " GB"}'
```

Assessment:
- **< 8 GB**: BLOCKER — "Hive Mind requires at least 8 GB of RAM. Claude Code, Neo4j, and Docker overhead will not fit."
- **8-15 GB**: WARNING — "8 GB is tight. You can run a minimal deployment (gateway + one mind + Neo4j) but adding voice, Ollama, or multiple minds will cause swapping. 16 GB recommended."
- **16+ GB**: OK — "Sufficient for a full deployment with multiple minds and services."

## Step 4 — GPU Detection

### NVIDIA
```bash
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null
```

If found:
- Record GPU model, driver version, and VRAM
- Check for nvidia-container-toolkit: `dpkg -l | grep nvidia-container-toolkit 2>/dev/null || rpm -qa | grep nvidia-container-toolkit 2>/dev/null`
- If toolkit missing: WARNING — "NVIDIA GPU detected but nvidia-container-toolkit is not installed. Docker GPU access requires it. Install: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
- Recommend profile: `gpu-nvidia`

### AMD
```bash
rocm-smi 2>/dev/null || lspci 2>/dev/null | grep -i 'vga\|3d\|display' | grep -i amd
```

If found:
- Note ROCm status
- Recommend profile: `gpu-amd`
- WARNING if ROCm is not installed: "AMD GPU detected but ROCm is not installed. GPU acceleration requires ROCm. See https://rocm.docs.amd.com/"

### No GPU
If neither NVIDIA nor AMD GPU is found:
- Recommend profile: `cpu-only`
- Note: "Voice server will use CPU-only models (slower inference). Ollama will run on CPU (functional but slower for large models)."

## Step 5 — Docker

```bash
docker info 2>&1 | head -5
docker compose version 2>&1
```

**Docker not found:**
Provide OS-specific install instructions:
- **Ubuntu/Debian**: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`
- **Fedora/RHEL**: `sudo dnf install docker-ce docker-compose-plugin`
- **macOS**: "Install Docker Desktop from https://docker.com/products/docker-desktop"
- **WSL**: "Install Docker Desktop for Windows with WSL integration enabled"

This is a BLOCKER — cannot proceed without Docker.

**Docker Compose not found or v1:**
- If `docker-compose` (hyphenated) exists but `docker compose` (space) does not: "Docker Compose v1 detected. Hive Mind requires v2+. Update Docker or install the compose plugin."
- BLOCKER.

**Docker running but permission denied:**
- "Docker is installed but your user cannot access the Docker socket. Run: `sudo usermod -aG docker $USER` and log out/back in."

## Step 6 — Git

```bash
git --version 2>&1
```

**Git not found:**
- **Ubuntu/Debian**: `sudo apt install git`
- **Fedora/RHEL**: `sudo dnf install git`
- **macOS**: `xcode-select --install` or `brew install git`
- BLOCKER.

## Step 7 — Disk Space

```bash
df -h . | tail -1
```

Parse available space:
- **< 10 GB**: BLOCKER — "Less than 10 GB free. Docker images alone need ~5 GB. Add models and data, you need more space."
- **10-20 GB**: WARNING — "Tight on space. Enough for a minimal deployment but Ollama models and voice models need more. Consider freeing space."
- **20+ GB**: OK.

## Step 8 — Compose Profile Recommendation

Based on GPU detection, recommend a profile:

| Detection | Profile | Notes |
|-----------|---------|-------|
| NVIDIA GPU + toolkit | `gpu-nvidia` | Full GPU acceleration for voice and Ollama |
| AMD GPU + ROCm | `gpu-amd` | GPU acceleration via ROCm |
| No GPU | `cpu-only` | All services run on CPU. Voice uses smaller models. |

Store the recommendation for `/setup-config` to use.

## Step 9 — Summary Report

Print a summary table:

```
Hive Mind Prerequisites Report
===============================

OS:             Ubuntu 24.04 LTS (Linux, x86_64)
WSL:            No
RAM:            32 GB                                    [OK]
GPU:            NVIDIA GeForce RTX 3080 (10 GB VRAM)     [OK]
  Driver:       535.183.01
  Toolkit:      nvidia-container-toolkit installed        [OK]
Docker:         Docker 27.1.1, Compose v2.29.1           [OK]
Git:            git 2.43.0                               [OK]
Disk:           145 GB available                         [OK]

Recommended compose profile: gpu-nvidia

Blockers:       None
Warnings:       None

Ready to proceed with /setup-config
```

If there are blockers, list them clearly and do NOT suggest proceeding:
```
BLOCKERS (must fix before continuing):
  1. Docker is not installed — see install instructions above
  2. Less than 8 GB RAM — add more memory or use a different machine

Fix blockers and run /setup-prerequisites again.
```
