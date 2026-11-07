"""Plan and apply service installation for the Python relay client.

``build_install_plan`` is side-effect free: it returns the files to write and
the commands to run.  ``apply_install_plan`` performs them.  Tests and
``--dry-run``/``--print`` use the plan alone, so generating an installer never
touches a live unit or Scheduled Task.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service import (
    SERVICE_NAME,
    ServiceSpec,
    render_systemd_unit,
    render_windows_task_script,
    systemd_unit_path,
    windows_task_script_path,
)


class InstallError(RuntimeError):
    """Raised when an install step fails."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    content: str
    mode: int = 0o644


@dataclass(frozen=True)
class InstallPlan:
    platform: str
    artifacts: tuple[Artifact, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    notes: tuple[str, ...] = ()

    def render(self) -> str:
        """Human-readable plan used by ``--dry-run``."""
        lines = [f"platform: {self.platform}"]
        for artifact in self.artifacts:
            lines.append(f"would write: {artifact.path}")
        for command in self.commands:
            lines.append("would run: " + " ".join(command))
        for note in self.notes:
            lines.append(f"note: {note}")
        if not self.artifacts and not self.commands:
            lines.append("nothing to do")
        return "\n".join(lines)

    @property
    def primary_content(self) -> str:
        return self.artifacts[0].content if self.artifacts else ""


def build_install_plan(
    platform: str,
    spec: ServiceSpec,
    *,
    unit_dir: str | os.PathLike[str] | None = None,
    appdata: str | os.PathLike[str] | None = None,
) -> InstallPlan:
    """Build the install plan for ``linux`` or ``windows`` without acting."""
    normalized = (platform or "").strip().lower()
    if normalized == "linux":
        path = systemd_unit_path(spec.name, config_home=unit_dir)
        return InstallPlan(
            platform="linux",
            artifacts=(Artifact(path=path, content=render_systemd_unit(spec)),),
            commands=(
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", "--now", f"{spec.name}.service"),
            ),
            notes=(
                "user unit — no root required; it starts with the graphical session",
                "for a headless box also run: loginctl enable-linger $USER",
            ),
        )
    if normalized == "windows":
        path = windows_task_script_path(appdata=appdata)
        return InstallPlan(
            platform="windows",
            artifacts=(
                Artifact(
                    path=path,
                    content=render_windows_task_script(spec),
                    mode=0o600,
                ),
            ),
            commands=(
                (
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(path),
                ),
            ),
            notes=(
                "per-user logon Scheduled Task — no administrator rights required",
                f"remove with: Unregister-ScheduledTask -TaskName '{SERVICE_NAME}' -Confirm:$false",
            ),
        )
    raise InstallError(
        f"unsupported install platform {platform!r}; expected linux or windows"
    )


def apply_install_plan(
    plan: InstallPlan,
    *,
    runner: Callable[..., Any] | None = None,
) -> list[Path]:
    """Write artifacts and run commands; returns the paths written."""
    written: list[Path] = []
    for artifact in plan.artifacts:
        artifact.path.parent.mkdir(parents=True, exist_ok=True)
        artifact.path.write_text(artifact.content, encoding="utf-8", newline="\n")
        try:
            os.chmod(artifact.path, artifact.mode)
        except OSError:  # pragma: no cover - exotic filesystems
            pass
        written.append(artifact.path)

    run = runner or subprocess.run
    for command in plan.commands:
        try:
            completed = run(list(command), check=False)
        except FileNotFoundError as exc:
            raise InstallError(f"cannot run {command[0]!r}: {exc}") from exc
        returncode = getattr(completed, "returncode", 0)
        if returncode != 0:
            detail = ""
            stderr = getattr(completed, "stderr", None)
            if stderr:
                detail = f": {stderr}"
            raise InstallError(
                f"command failed ({returncode}): {' '.join(command)}{detail}"
            )
    return written


__all__ = [
    "Artifact",
    "InstallError",
    "InstallPlan",
    "apply_install_plan",
    "build_install_plan",
]
