"""Bounded subprocess runner shared by every Linux platform adapter.

All adapter shell-outs go through :class:`CommandRunner`.  Tests inject a fake
runner and assert on the exact argv a Hyprland/X11 session would receive; the
default :class:`SubprocessRunner` is dependency-free, enforces a per-command
timeout, kills the whole process group on timeout/cancel, and caps captured
stdout so one runaway command cannot exhaust the daemon's memory.

``ran`` distinguishes "the executable could not be started" (missing binary,
``ran=False``) from "it started and failed" (``ran=True`` + non-zero
``exit_code``).  Adapters rely on that distinction to report
``missing_dependency`` rather than a generic input failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_MS = 15_000
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_BUFFER_BYTES = 64 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024


@dataclass
class CommandResult:
    ran: bool
    exit_code: int
    stdout: bytes
    stderr: str
    timed_out: bool = False
    too_large: bool = False

    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")


class CommandRunner(ABC):
    """Injectable process boundary."""

    @abstractmethod
    async def run(
        self,
        cmd: str,
        args: Sequence[str],
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        stdin: str | bytes | None = None,
        env: Mapping[str, str] | None = None,
        max_bytes: int = MAX_TEXT_BYTES,
        signal_event: asyncio.Event | None = None,
    ) -> CommandResult: ...

    async def text(
        self,
        cmd: str,
        args: Sequence[str],
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        stdin: str | bytes | None = None,
        env: Mapping[str, str] | None = None,
        signal_event: asyncio.Event | None = None,
    ) -> CommandResult:
        return await self.run(
            cmd,
            args,
            timeout_ms=timeout_ms,
            stdin=stdin,
            env=env,
            max_bytes=MAX_TEXT_BYTES,
            signal_event=signal_event,
        )

    async def buffer(
        self,
        cmd: str,
        args: Sequence[str],
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        stdin: str | bytes | None = None,
        env: Mapping[str, str] | None = None,
        signal_event: asyncio.Event | None = None,
    ) -> CommandResult:
        return await self.run(
            cmd,
            args,
            timeout_ms=timeout_ms,
            stdin=stdin,
            env=env,
            max_bytes=MAX_BUFFER_BYTES,
            signal_event=signal_event,
        )


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover - the relay client's Linux path is the target
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


def _exit_code(process: asyncio.subprocess.Process) -> int:
    code = process.returncode
    if code is None:
        return -1
    return 128 + (-code) if code < 0 else code


async def _drain(
    stream: asyncio.StreamReader | None,
    cap: int,
    limit_hit: asyncio.Event,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        remaining = cap - total
        if remaining <= 0:
            truncated = True
            limit_hit.set()
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total = cap
            truncated = True
            limit_hit.set()
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _task_result(task: asyncio.Task[Any], default: Any) -> Any:
    if not task.done() or task.cancelled():
        return default
    try:
        return task.result()
    except Exception:  # noqa: BLE001 - a killed drain has no useful result
        return default


class SubprocessRunner(CommandRunner):
    """Default runner: ``create_subprocess_exec`` with no shell."""

    async def run(
        self,
        cmd: str,
        args: Sequence[str],
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        stdin: str | bytes | None = None,
        env: Mapping[str, str] | None = None,
        max_bytes: int = MAX_TEXT_BYTES,
        signal_event: asyncio.Event | None = None,
    ) -> CommandResult:
        if signal_event is not None and signal_event.is_set():
            return CommandResult(False, -1, b"", "aborted", timed_out=False)

        argv = [cmd, *[str(arg) for arg in args]]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env) if env is not None else None,
                start_new_session=sys.platform != "win32",
            )
        except FileNotFoundError as exc:
            return CommandResult(False, -1, b"", str(exc))
        except (NotADirectoryError, PermissionError, OSError) as exc:
            return CommandResult(True, -1, b"", str(exc))

        limit_hit = asyncio.Event()
        stdout_task = asyncio.create_task(
            _drain(process.stdout, max_bytes, limit_hit), name="relay-cmd-stdout"
        )
        stderr_task = asyncio.create_task(
            _drain(process.stderr, MAX_STDERR_BYTES, limit_hit), name="relay-cmd-stderr"
        )
        wait_task = asyncio.create_task(process.wait(), name="relay-cmd-wait")
        limit_task = asyncio.create_task(limit_hit.wait(), name="relay-cmd-limit")

        async def _feed_stdin() -> None:
            if process.stdin is None:
                return
            try:
                if stdin is not None:
                    payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
                    process.stdin.write(payload)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

        stdin_task = asyncio.create_task(_feed_stdin(), name="relay-cmd-stdin")
        timed_out = False
        try:
            done, _pending = await asyncio.wait(
                {wait_task, limit_task},
                timeout=max(1, timeout_ms) / 1000.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            timed_out = wait_task not in done and limit_task not in done
            too_large = limit_task in done
            if timed_out or too_large:
                _kill_process_group(process)
            low_priority: list[Any] = [
                task
                for task in (stdout_task, stderr_task, stdin_task)
                if not task.done()
            ]
            cancel_remaining = timed_out or too_large
            try:
                await asyncio.wait_for(
                    asyncio.gather(*low_priority, return_exceptions=True),
                    timeout=2.0 if cancel_remaining else 5.0,
                )
            except TimeoutError:
                for task in low_priority:
                    task.cancel()
                await asyncio.gather(*low_priority, return_exceptions=True)
            if not wait_task.done():
                await wait_task
        except asyncio.CancelledError:
            _kill_process_group(process)
            for task in (stdout_task, stderr_task, stdin_task, wait_task, limit_task):
                task.cancel()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                stdin_task,
                wait_task,
                limit_task,
                return_exceptions=True,
            )
            raise
        finally:
            limit_task.cancel()
            try:
                await limit_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        stdout, stdout_truncated = _task_result(stdout_task, (b"", False))
        stderr_text, stderr_truncated = _task_result(stderr_task, (b"", False))
        details = [stderr_text.decode("utf-8", "replace")]
        if timed_out:
            details.append(f"[timed out after {timeout_ms}ms]")
        if stdout_truncated:
            details.append(f"[output exceeded {max_bytes} bytes]")
        if stderr_truncated:
            details.append(f"[stderr exceeded {MAX_STDERR_BYTES} bytes]")
        return CommandResult(
            ran=True,
            exit_code=_exit_code(process),
            stdout=stdout,
            stderr="\n".join(part for part in details if part),
            timed_out=timed_out,
            too_large=stdout_truncated,
        )


def which_binary(
    name: str,
    path_value: str | None = None,
    home: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve an executable on PATH plus the usual user-local install dirs."""

    if not name:
        return None
    environment = env if env is not None else os.environ
    home = home or environment.get("HOME") or os.path.expanduser("~")
    if os.path.isabs(name):
        candidates = [name]
    else:
        path_value = (
            path_value if path_value is not None else environment.get("PATH", "")
        )
        directories = [entry for entry in path_value.split(os.pathsep) if entry]
        candidates = [os.path.join(directory, name) for directory in directories]
        candidates.extend(
            [
                os.path.join(home, ".local", "bin", name),
                os.path.join(home, ".hermes", "bin", name),
            ]
        )
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name, path=path_value) if not os.path.isabs(name) else None


def parse_json(text: str) -> Any:
    trimmed = (text or "").strip()
    if not trimmed:
        return None
    try:
        return json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return None


_CURSOR_RE = re.compile(r"(-?\d+)\s*,\s*(-?\d+)")


def parse_cursor_pos(text: str) -> tuple[int, int] | None:
    match = _CURSOR_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "MAX_BUFFER_BYTES",
    "MAX_TEXT_BYTES",
    "CommandResult",
    "CommandRunner",
    "SubprocessRunner",
    "parse_cursor_pos",
    "parse_json",
    "which_binary",
]
