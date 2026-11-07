"""Shell tool handlers for the Python relay client.

Implements the two shell entry points from ``desktop/src/tools/handlers``:

* ``desktop_terminal``   — bounded shell command (bash -lc / cmd /c)
* ``desktop_powershell`` — private UTF-8 script file, no cmd.exe quoting
                           (Windows hosts; ``pwsh`` also works elsewhere)

Both handlers terminate their child on timeout or task cancellation, so a
relay disconnect cannot leave a runaway command behind.

Result shapes match the TypeScript client exactly:
``{stdout, stderr, exit_code, duration_ms}`` for terminal and the richer
``{stdout, stderr, exit_code, duration_ms, shell, truncated, output}`` for
PowerShell.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..dispatch import ToolContext, ToolError

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 10 * 60_000
POWERSHELL_MAX_OUTPUT_BYTES = 1024 * 1024


def _require_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or value == "":
        raise ToolError(f'missing or invalid "{name}" argument')
    return value


def _optional_str(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and value else None


def resolve_timeout_ms(args: dict[str, Any]) -> int:
    """``timeout_ms`` wins over ``timeout`` (seconds), both clamped to 10 min."""
    raw_ms = args.get("timeout_ms")
    if isinstance(raw_ms, (int, float)) and not isinstance(raw_ms, bool) and raw_ms > 0:
        return min(int(raw_ms), MAX_TIMEOUT_MS)
    raw_seconds = args.get("timeout")
    if isinstance(raw_seconds, (int, float)) and not isinstance(raw_seconds, bool) and raw_seconds > 0:
        return min(int(raw_seconds * 1000), MAX_TIMEOUT_MS)
    return DEFAULT_TIMEOUT_MS


def _shell_command(command: str) -> tuple[str, list[str]]:
    if sys.platform == "win32":
        return "cmd", ["/c", command]
    return "bash", ["-lc", command]


def _kill_process(process: asyncio.subprocess.Process) -> None:
    """Kill the child and, on POSIX, its process group."""
    if process.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), 9)
        else:
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
    # Match Node: a signal-terminated child reports 128 + signal.
    return 128 + (-code) if code < 0 else code


async def _spawn(
    argv: list[str],
    *,
    cwd: str,
    stdin_data: bytes | None = None,
) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
    except FileNotFoundError as exc:
        raise ToolError(f"shell not found: {argv[0]}") from exc
    except NotADirectoryError as exc:
        raise ToolError(f"working directory does not exist: {cwd}") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc


async def _communicate(
    process: asyncio.subprocess.Process,
    timeout_ms: int,
    ctx: ToolContext,
    stdin_data: bytes | None = None,
) -> tuple[bytes, bytes]:
    try:
        if stdin_data is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin_data)
                await process.stdin.drain()
            finally:
                process.stdin.close()
        return await asyncio.wait_for(
            process.communicate(), timeout=timeout_ms / 1000.0
        )
    except asyncio.TimeoutError as exc:
        _kill_process(process)
        await process.wait()
        raise ToolError(f"timed out after {timeout_ms}ms") from exc
    except asyncio.CancelledError:
        _kill_process(process)
        await process.wait()
        raise
    finally:
        if ctx.cancelled:
            _kill_process(process)


async def terminal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run a shell command under the platform shell and capture its output."""
    command = _require_str(args, "command")
    cwd = _optional_str(args, "cwd") or ctx.cwd
    timeout_ms = resolve_timeout_ms(args)
    shell, shell_args = _shell_command(command)

    started = time.monotonic()
    process = await _spawn([shell, *shell_args], cwd=cwd)
    stdout, stderr = await _communicate(process, timeout_ms, ctx)
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": _exit_code(process),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# ── desktop_powershell ──────────────────────────────────────────────────────


def _probe_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _pick_powershell(prefer: str | None) -> str:
    if prefer == "pwsh":
        if _probe_binary("pwsh"):
            return "pwsh"
        raise ToolError("pwsh requested but not on PATH")
    if prefer == "powershell":
        if sys.platform != "win32":
            raise ToolError("powershell (Windows PowerShell) is only available on Windows")
        if _probe_binary("powershell"):
            return "powershell"
        raise ToolError("powershell requested but not on PATH")
    if _probe_binary("pwsh"):
        return "pwsh"
    if sys.platform == "win32" and _probe_binary("powershell"):
        return "powershell"
    raise ToolError(
        "no PowerShell on PATH (tried pwsh"
        + (", powershell" if sys.platform == "win32" else "")
        + ")"
    )


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> tuple[bytes, int, int]:
    """Read a stream, retaining at most ``cap`` bytes but counting them all."""
    if stream is None:
        return b"", 0, 0
    chunks: list[bytes] = []
    total = 0
    captured = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if captured < cap:
            take = chunk[: cap - captured]
            chunks.append(take)
            captured += len(take)
    return b"".join(chunks), total, captured


async def powershell(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run a PowerShell script through a private temporary UTF-8 file."""
    if sys.platform != "win32":
        # Advertised only on Windows, but a relay could still address us.
        raise ToolError("desktop_powershell is only available on Windows clients")
    script = _require_str(args, "script")
    cwd = _optional_str(args, "cwd") or ctx.cwd
    timeout_ms = resolve_timeout_ms(args)
    shell = _pick_powershell(_optional_str(args, "prefer"))

    directory = Path(tempfile.mkdtemp(prefix="hermes-relay-powershell-"))
    script_path = directory / "invoke.ps1"
    wrapped = "\n".join(
        [
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
            "$OutputEncoding = [Console]::OutputEncoding",
            script,
            "if ($null -ne (Get-Variable LASTEXITCODE -ErrorAction SilentlyContinue)) "
            "{ exit $LASTEXITCODE }",
        ]
    )
    try:
        os.chmod(directory, 0o700)
        script_path.write_text(wrapped, encoding="utf-8")
        os.chmod(script_path, 0o600)
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise ToolError(f"cannot write PowerShell script: {exc}") from exc

    shell_args = ["-NoProfile", "-NonInteractive"]
    if sys.platform == "win32":
        shell_args.extend(["-ExecutionPolicy", "Bypass"])
    shell_args.extend(["-File", str(script_path)])

    started = time.monotonic()
    try:
        process = await _spawn([shell, *shell_args], cwd=cwd)
    except ToolError:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    stdout_task: asyncio.Task[tuple[bytes, int, int]] | None = None
    stderr_task: asyncio.Task[tuple[bytes, int, int]] | None = None
    try:
        stdout_task = asyncio.create_task(_read_capped(process.stdout, POWERSHELL_MAX_OUTPUT_BYTES))
        stderr_task = asyncio.create_task(_read_capped(process.stderr, POWERSHELL_MAX_OUTPUT_BYTES))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError as exc:
            _kill_process(process)
            await process.wait()
            raise ToolError(f"timed out after {timeout_ms}ms") from exc
        except asyncio.CancelledError:
            _kill_process(process)
            await process.wait()
            raise
        stdout, stdout_total, stdout_captured = await stdout_task
        stderr, stderr_total, stderr_captured = await stderr_task
    finally:
        for task in (stdout_task, stderr_task):
            if task is not None and not task.done():
                task.cancel()
        shutil.rmtree(directory, ignore_errors=True)

    stdout_truncated = stdout_total > stdout_captured
    stderr_truncated = stderr_total > stderr_captured
    truncated = stdout_truncated or stderr_truncated
    result: dict[str, Any] = {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": _exit_code(process),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "shell": shell,
        "truncated": truncated,
        "output": {
            "limit_bytes_per_stream": POWERSHELL_MAX_OUTPUT_BYTES,
            "stdout": {
                "bytes": stdout_total,
                "captured_bytes": stdout_captured,
                "truncated": stdout_truncated,
            },
            "stderr": {
                "bytes": stderr_total,
                "captured_bytes": stderr_captured,
                "truncated": stderr_truncated,
            },
        },
    }
    if truncated:
        result["truncation_reason"] = "output_limit"
    return result


def handlers(*, include_powershell: bool = True) -> dict[str, Any]:
    """Handler map for the shell tool family.

    ``desktop_powershell`` is Windows-only per the porting plan, so the CLI
    omits it (and therefore does not advertise it) on other platforms.
    """
    mapping: dict[str, Any] = {"desktop_terminal": terminal}
    if include_powershell and sys.platform == "win32":
        mapping["desktop_powershell"] = powershell
    return mapping


__all__ = ["handlers", "powershell", "resolve_timeout_ms", "terminal"]
