<#
.SYNOPSIS
  Install Hermes-Relay's desktop agent as a logon Scheduled Task.

.DESCRIPTION
  Desktop automation needs the interactive desktop, so this does not create a
  Windows service. It registers an "At log on" task for the current user with
  restart-on-failure, then starts it immediately. The daemon reconnects using
  the relay transport's existing backoff.

.PARAMETER Bin
  Absolute path to hermes-relay.exe / hermes-relay.cmd. Defaults to PATH.

.PARAMETER Status
  Show the task state and `hermes-relay daemon --status`.

.PARAMETER Uninstall
  Stop and remove the task.
#>
param(
  [string]$Bin = $env:HERMES_RELAY_BIN,
  [switch]$Status,
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'HermesRelayDesktop'
$HermesDir = Join-Path $env:USERPROFILE '.hermes'
$LogPath = Join-Path $HermesDir 'daemon-service.log'

function Write-Info([string]$Message) { Write-Host "  $Message" }
function Fail([string]$Message) { Write-Error $Message; exit 1 }

if ($Status) {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $task) { Write-Info "task $TaskName is not installed"; exit 1 }
  Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
  Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime
  $cli = if ($Bin) { $Bin } else { (Get-Command hermes-relay -ErrorAction SilentlyContinue).Source }
  if ($cli) { & $cli daemon --status }
  exit 0
}

if ($Uninstall) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Info "removed scheduled task $TaskName"
  exit 0
}

if (-not $Bin) {
  $command = Get-Command hermes-relay -ErrorAction SilentlyContinue
  if ($command) { $Bin = $command.Source }
}
if (-not $Bin) { Fail 'hermes-relay not found on PATH. Pass -Bin C:\path\to\hermes-relay.exe' }
if (-not (Test-Path -LiteralPath $Bin)) { Fail "binary not found: $Bin" }

New-Item -ItemType Directory -Force -Path $HermesDir | Out-Null

# Scheduled tasks do not have a console. Redirect both streams to a log so
# connection failures are diagnosable without Event Viewer.
$escapedBin = $Bin.Replace("'", "''")
$escapedLog = $LogPath.Replace("'", "''")
if ($Bin -match '\.(cmd|bat)$') {
  $inner = "& '$escapedBin' daemon *>> '$escapedLog'"
  $argument = "/c ""$inner"""
  $action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument $argument
} else {
  $argument = "-NoProfile -WindowStyle Hidden -Command ""& '$escapedBin' daemon *>> '$escapedLog'"""
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Principal $principal `
  -Description 'Hermes Relay desktop agent — lets the server-hosted Hermes operate this computer after pairing.' `
  -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$task = Get-ScheduledTask -TaskName $TaskName
if ($task.State -eq 'Running') {
  Write-Info "installed and started scheduled task $TaskName"
  Write-Info "log: $LogPath"
  Write-Info "status: hermes-relay daemon --status"
} else {
  Write-Warning "task installed but state is $($task.State). Check $LogPath"
  exit 1
}
