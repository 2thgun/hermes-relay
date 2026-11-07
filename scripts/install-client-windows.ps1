<#
.SYNOPSIS
    Install the headless Hermes-Relay Python client as a per-user logon task.

.DESCRIPTION
    Registers a hidden Scheduled Task that starts the Python client when the
    current user logs on, restarts it on failure, and needs no administrator
    rights. This installs the Python entry point — not the Bun/TypeScript
    client — and does not change any existing service.

.PARAMETER Print
    Print the generated Scheduled Task registration script and exit.

.PARAMETER DryRun
    Print the files and commands that would be used, then exit.

.PARAMETER Url
    Relay WebSocket URL to embed in the task arguments.

.PARAMETER State
    Client store path to embed (default: $HERMES_HOME\relay\client.json).

.PARAMETER Python
    Python interpreter to embed (default: pythonw.exe next to this Python).

.EXAMPLE
    .\install-client-windows.ps1 -Print -Url wss://relay.example:8767
#>
[CmdletBinding()]
param(
    [switch]$Print,
    [switch]$DryRun,
    [string]$Url,
    [string]$State,
    [string]$Python
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot

$arguments = @("install", "--platform", "windows")
if ($Print) {
    $arguments += "--print"
} elseif ($DryRun) {
    $arguments += "--dry-run"
}
if ($Url) { $arguments += @("--url", $Url) }
if ($State) { $arguments += @("--state", $State) }
if ($Python) { $arguments += @("--python", $Python) }

$client = Get-Command hermes-relay-client -ErrorAction SilentlyContinue
if ($client) {
    $command = $client.Source
    $prefix = @()
} else {
    if ($Python) {
        $interpreter = $Python
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $interpreter = (Get-Command python).Source
    } else {
        throw "python not found on PATH; pass -Python C:\Path\python.exe"
    }
    $command = $interpreter
    $prefix = @("-m", "plugin.relay.device")
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$repoRoot;$env:PYTHONPATH" } else { $repoRoot }
}

& $command @prefix @arguments
exit $LASTEXITCODE
