# Start the web terminal natively on Windows.
#
# The compose file ran this as a container behind Caddy. Neither is here:
# this box has no Docker by choice, and there is no TLS front door to
# terminate because nothing but this machine's own browser ever reaches
# it. So the two things Caddy was doing that mattered are gone with it —
# FORWARDED_ALLOW_IPS existed only because Caddy proxied in the clear and
# uvicorn had to be told to trust its X-Forwarded-Proto, and there is no
# proxy now.
#
# Configuration is read from mini-boss's `.env`, not copied into a second
# one. The bearer token, the auth secret and the seed login have to agree
# across both processes, and two files that must agree are two files that
# will eventually disagree.

[CmdletBinding()]
param(
    [string]$MindRepo = 'C:\Users\dastewart\source\repos\mini-boss'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$envFile = Join-Path $MindRepo '.env'
if (-not (Test-Path $envFile)) {
    throw "No .env at $envFile. The terminal reads the mind's configuration; they must agree."
}

$mind = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    $text = $line.Trim()
    if ($text -eq '' -or $text.StartsWith('#')) { continue }
    $split = $text.IndexOf('=')
    if ($split -lt 1) { continue }
    $key = $text.Substring(0, $split).Trim()
    $value = $text.Substring($split + 1).Trim()
    if ($value.Length -ge 2 -and
        (($value.StartsWith('"') -and $value.EndsWith('"')) -or
         ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $mind[$key] = $value
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

# Where the mind is. Loopback, because the terminal and the mind are two
# processes on one machine now rather than two containers on a network.
$mindPort = if ($mind['MIND_PORT']) { $mind['MIND_PORT'] } else { '8431' }
$env:GATEWAY_API_URL = "http://127.0.0.1:$mindPort"

# The image sends this as `Authorization: Bearer` on both its HTTP calls
# and its WebSocket attach, which is exactly what guards every route on
# the mind.
$env:COMMS_BEARER_TOKEN = $mind['MIND_API_TOKEN']

# What the mind proves itself with when it renames a conversation from
# Telegram, and deliberately a *different* secret from the one above.
# That one opens every route on the mind, including the attach that types
# into a live pane; a rename should not be able to reach any of it. This
# one writes label text and nothing else, so a copy that escapes is worth
# exactly one renamed conversation.
$env:TERMINAL_LABELS_TOKEN = $mind['TERMINAL_LABELS_TOKEN']

$env:TERMINAL_HOST = if ($env:TERMINAL_HOST) { $env:TERMINAL_HOST } else { '127.0.0.1' }
$env:TERMINAL_PORT = if ($env:TERMINAL_PORT) { $env:TERMINAL_PORT } else { '8432' }

$env:HIVE_SECRET_KEY = $mind['HIVE_SECRET_KEY']
$env:HIVE_SEED_USERNAME = $mind['HIVE_SEED_USERNAME']
$env:HIVE_SEED_PASSWORD = $mind['HIVE_SEED_PASSWORD']
# Host-only cookie: there is no sibling site to share it with here.
$env:HIVE_COOKIE_DOMAIN = ''

# Every request to a file as well as the console. Without this the
# service is unobservable: a button that "does nothing" cannot be told
# apart from a request that never arrived, and the only way to find out
# is to guess.
$logDir = Join-Path $env:USERPROFILE '.mini-boss'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$logPath = Join-Path $logDir 'terminal.log'
if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 8MB)) {
    Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
}
"=== $(Get-Date -Format o) starting terminal ===" | Add-Content -LiteralPath $logPath

Write-Host "terminal: http://$($env:TERMINAL_HOST):$($env:TERMINAL_PORT) -> mind at $($env:GATEWAY_API_URL) log: $logPath"
& $python (Join-Path $PSScriptRoot 'src\main.py') 2>&1 |
    Tee-Object -FilePath $logPath -Append
exit $LASTEXITCODE
