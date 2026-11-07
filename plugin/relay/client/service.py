"""Install-artifact generators for the Python relay client.

The generators are pure string builders so they can be tested on any OS: the
Linux unit and the Windows Scheduled Task registration script are produced
here, and :mod:`plugin.relay.client.installer` decides whether to print them
or write/execute them.  Nothing in this module touches an installed service.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "hermes-relay-client"
SERVICE_DESCRIPTION = "Hermes-Relay client (desktop tools)"
WINDOWS_TASK_NAME = "Hermes Relay Client"
WINDOWS_TASK_DESCRIPTION = "Hermes-Relay Python client (desktop tools)"

# Characters that are safe unquoted in an ExecStart word.  ``%`` and ``$``
# are excluded because systemd expands them even without quotes.
_SYSTEMD_SAFE = re.compile(r"^[A-Za-z0-9_@+=:,./-]+$")


@dataclass(frozen=True)
class ServiceSpec:
    """Everything the generated unit/task needs to launch the client."""

    command: tuple[str, ...]
    working_directory: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    name: str = SERVICE_NAME

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("service command must not be empty")


def build_client_command(
    *,
    python: str | None = None,
    executable: str | None = None,
    url: str | None = None,
    state: str | os.PathLike[str] | None = None,
    extra: Iterable[str] = (),
) -> tuple[str, ...]:
    """Build the argv a service should run.

    Preference order: an explicit console-script path, an explicit Python
    interpreter (``-m plugin.relay.client``), the installed
    ``hermes-relay-client`` script, then this interpreter with ``-m``.
    """
    if executable:
        head: tuple[str, ...] = (str(executable),)
    elif python:
        head = (str(python), "-m", "plugin.relay.client")
    else:
        found = shutil.which("hermes-relay-client")
        head = (found,) if found else (sys.executable, "-m", "plugin.relay.client")

    args: list[str] = ["run"]
    if url:
        args.extend(["--url", str(url)])
    if state:
        args.extend(["--state", str(state)])
    args.extend(str(item) for item in extra)
    return (*head, *args)


def default_workdir() -> str:
    return str(Path.home())


def systemd_unit_path(
    name: str = SERVICE_NAME,
    *,
    config_home: str | os.PathLike[str] | None = None,
) -> Path:
    """``$XDG_CONFIG_HOME/systemd/user/<name>.service`` (user unit)."""
    if config_home is not None:
        base = Path(config_home).expanduser()
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / f"{name}.service"


def windows_task_script_path(
    *,
    appdata: str | os.PathLike[str] | None = None,
) -> Path:
    """Per-user location for the generated Scheduled Task registration script."""
    if appdata is not None:
        base = Path(appdata).expanduser()
    else:
        raw = os.environ.get("APPDATA")
        base = Path(raw) if raw else Path.home() / "AppData" / "Roaming"
    return base / "hermes-relay" / "install-client-task.ps1"


def _systemd_arg(arg: str) -> str:
    """Quote one ExecStart argument for systemd.

    systemd treats ``%`` as a specifier and ``$`` as variable expansion even
    inside double quotes, so both are doubled.  Plain paths/flags stay
    unquoted for readability; anything with whitespace or shell punctuation
    is double-quoted.
    """
    text = str(arg)
    if _SYSTEMD_SAFE.match(text):
        return text
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("%", "%%").replace("$", "$$")
    return f'"{text}"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    """Render a systemd **user** unit bound to ``graphical-session.target``."""
    lines = [
        "[Unit]",
        f"Description={SERVICE_DESCRIPTION}",
        "Documentation=https://github.com/Codename-11/hermes-relay/blob/main/docs/python-client.md",
        "After=graphical-session.target",
        "PartOf=graphical-session.target",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_systemd_arg(part) for part in spec.command),
        "Restart=on-failure",
        "RestartSec=5s",
        "KillMode=mixed",
        "TimeoutStopSec=10s",
        # A client is a session service; it must not inherit a stale env from
        # an unrelated login shell, but a missing .env is not an error.
        "Environment=HERMES_RELAY_ROLE=client",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    if spec.working_directory:
        lines.append(f"WorkingDirectory={spec.working_directory}")
    for key, value in spec.environment:
        lines.append(f"Environment={key}={value}")
    lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=graphical-session.target",
            "",
        ]
    )
    return "\n".join(lines)


def _ps_quote(value: str) -> str:
    """Single-quote a PowerShell literal (single quotes are doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def _windows_arguments(command: Sequence[str]) -> str:
    """Join argv[1:] into one Windows command line string."""
    parts = []
    for part in command[1:]:
        text = str(part)
        if not text:
            parts.append('""')
        elif any(ch.isspace() for ch in text) or '"' in text:
            parts.append('"' + text.replace('"', '\\"') + '"')
        else:
            parts.append(text)
    return " ".join(parts)


def render_windows_task_script(
    spec: ServiceSpec,
    *,
    task_name: str = WINDOWS_TASK_NAME,
) -> str:
    """Render a PowerShell script registering a hidden per-user logon task.

    The task runs at logon as the current user (no admin rights), starts
    hidden, has no execution time limit, and restarts every minute on
    failure — the same supervision contract as the systemd unit.
    """
    program = str(spec.command[0])
    arguments = _windows_arguments(spec.command)
    lines = [
        "# Generated by `hermes-relay-client install --platform windows`.",
        "# Registers a per-user logon Scheduled Task; no administrator rights required.",
        "# Re-running replaces the task (-Force).",
        '$ErrorActionPreference = "Stop"',
        f"$taskName = {_ps_quote(task_name)}",
        f"$program = {_ps_quote(program)}",
        f"$arguments = {_ps_quote(arguments)}",
    ]
    if spec.working_directory:
        lines.append(f"$workDir = {_ps_quote(spec.working_directory)}")
        action = (
            "$action = New-ScheduledTaskAction -Execute $program "
            "-Argument $arguments -WorkingDirectory $workDir"
        )
    else:
        action = (
            "$action = New-ScheduledTaskAction -Execute $program -Argument $arguments"
        )
    lines.extend(
        [
            action,
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
            (
                "$settings = New-ScheduledTaskSettingsSet -Hidden "
                "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                "-ExecutionTimeLimit ([TimeSpan]::Zero) "
                "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) "
                "-StartWhenAvailable"
            ),
            (
                '$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" '
                "-LogonType Interactive -RunLevel Limited"
            ),
            (
                "Register-ScheduledTask -TaskName $taskName -Action $action "
                "-Trigger $trigger -Settings $settings -Principal $principal "
                f"-Description {_ps_quote(WINDOWS_TASK_DESCRIPTION)} -Force"
            ),
            "Write-Host \"Registered scheduled task '$taskName'.\"",
            "",
        ]
    )
    return "\n".join(lines)


def windows_python(python: str | None = None) -> str:
    """Prefer ``pythonw.exe`` on Windows so the task has no console window."""
    if python:
        return python
    exe = Path(sys.executable)
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


__all__ = [
    "SERVICE_DESCRIPTION",
    "SERVICE_NAME",
    "WINDOWS_TASK_DESCRIPTION",
    "WINDOWS_TASK_NAME",
    "ServiceSpec",
    "build_client_command",
    "default_workdir",
    "render_systemd_unit",
    "render_windows_task_script",
    "systemd_unit_path",
    "windows_python",
    "windows_task_script_path",
]
