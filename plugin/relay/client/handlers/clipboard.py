"""Clipboard bridge for the Python relay client.

Text-only, one-shot clipboard read/write (matching
``desktop/src/tools/handlers/clipboard.ts``):

* ``desktop_clipboard_read``  → ``{content: str, format: "text"}``
* ``desktop_clipboard_write`` → ``{wrote: <utf-8 byte count>}``

Platform dispatch is shell-outs only, so the client carries no native
dependencies:

* Windows — ``powershell Get-Clipboard -Raw`` / ``$input | Set-Clipboard``
* macOS   — ``pbpaste`` / ``pbcopy``
* Linux   — ``wl-paste``/``wl-copy`` when on Wayland, otherwise
            ``xclip -selection clipboard`` (with a fallback when the
            preferred binary is missing)

Both directions are capped at 10 MB; a larger clipboard is rejected rather
than streamed over the relay.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from ..dispatch import ToolContext, ToolError

CLIPBOARD_TIMEOUT_S = 5.0
MAX_CLIPBOARD_BYTES = 10 * 1024 * 1024


async def _capture(
    argv: list[str],
    *,
    stdin: bytes | None,
    timeout: float = CLIPBOARD_TIMEOUT_S,
) -> tuple[bytes, str, int]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
    except FileNotFoundError:
        raise ToolError(f"clipboard command not found: {argv[0]}") from None
    except OSError as exc:
        raise ToolError(str(exc)) from exc

    async def _read(stream: asyncio.StreamReader | None, cap: int) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        chunks: list[bytes] = []
        total = 0
        oversize = False
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                oversize = True
                chunks = []
                _kill(process)
                break
            chunks.append(chunk)
        return b"".join(chunks), oversize

    try:
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            finally:
                process.stdin.close()
        (stdout, oversize), (stderr, _), _ = await asyncio.wait_for(
            asyncio.gather(
                _read(process.stdout, MAX_CLIPBOARD_BYTES),
                _read(process.stderr, 64 * 1024),
                process.wait(),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        _kill(process)
        await process.wait()
        raise ToolError(f"clipboard command timed out after {timeout:.0f}s: {argv[0]}") from exc
    except asyncio.CancelledError:
        _kill(process)
        await process.wait()
        raise

    if oversize:
        raise ToolError(f"clipboard too large (>{MAX_CLIPBOARD_BYTES} bytes)")
    return stdout, stderr.decode("utf-8", errors="replace"), process.returncode or 0


def _kill(process: asyncio.subprocess.Process) -> None:
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


def _wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


async def _read_clipboard() -> tuple[bytes, str]:
    if sys.platform == "win32":
        stdout, stderr, code = await _capture(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
            stdin=None,
        )
        if code != 0:
            raise ToolError(f"powershell Get-Clipboard failed: {stderr.strip()[:500] or code}")
        if stdout.endswith(b"\r\n"):
            stdout = stdout[:-2]
        return stdout, "powershell Get-Clipboard"

    if sys.platform == "darwin":
        stdout, stderr, code = await _capture(["pbpaste"], stdin=None)
        if code != 0:
            raise ToolError(f"pbpaste failed: {stderr.strip()[:500] or code}")
        return stdout, "pbpaste"

    if sys.platform.startswith("linux"):
        if _wayland():
            try:
                stdout, stderr, code = await _capture(["wl-paste", "--no-newline"], stdin=None)
                if code != 0:
                    raise ToolError(f"wl-paste failed: {stderr.strip()[:500] or code}")
                return stdout, "wl-paste"
            except ToolError as exc:
                if "not found" not in str(exc):
                    raise
        stdout, stderr, code = await _capture(
            ["xclip", "-selection", "clipboard", "-o"], stdin=None
        )
        if code != 0:
            raise ToolError(
                "no clipboard reader available; install wl-clipboard or xclip "
                f"(xclip: {stderr.strip()[:300] or code})"
            )
        return stdout, "xclip -selection clipboard -o"

    raise ToolError(f"clipboard read not supported on platform: {sys.platform}")


async def _write_clipboard(content: str) -> tuple[int, str]:
    data = content.encode("utf-8")
    if len(data) > MAX_CLIPBOARD_BYTES:
        raise ToolError(
            f"content too large ({len(data)} bytes > {MAX_CLIPBOARD_BYTES} limit)"
        )

    if sys.platform == "win32":
        _, stderr, code = await _capture(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$input | Set-Clipboard",
            ],
            stdin=data,
        )
        if code != 0:
            raise ToolError(f"powershell Set-Clipboard failed: {stderr.strip()[:500] or code}")
        return len(data), "powershell Set-Clipboard"

    if sys.platform == "darwin":
        _, stderr, code = await _capture(["pbcopy"], stdin=data)
        if code != 0:
            raise ToolError(f"pbcopy failed: {stderr.strip()[:500] or code}")
        return len(data), "pbcopy"

    if sys.platform.startswith("linux"):
        if _wayland():
            try:
                _, stderr, code = await _capture(["wl-copy"], stdin=data)
                if code != 0:
                    raise ToolError(f"wl-copy failed: {stderr.strip()[:500] or code}")
                return len(data), "wl-copy"
            except ToolError as exc:
                if "not found" not in str(exc):
                    raise
        _, stderr, code = await _capture(
            ["xclip", "-selection", "clipboard"], stdin=data
        )
        if code != 0:
            raise ToolError(
                "no clipboard writer available; install wl-clipboard or xclip "
                f"(xclip: {stderr.strip()[:300] or code})"
            )
        return len(data), "xclip -selection clipboard"

    raise ToolError(f"clipboard write not supported on platform: {sys.platform}")


async def clipboard_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    stdout, _command = await _read_clipboard()
    return {"content": stdout.decode("utf-8", errors="replace"), "format": "text"}


async def clipboard_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError('missing or invalid "content" argument')
    wrote, _command = await _write_clipboard(content)
    return {"wrote": wrote}


def handlers() -> dict[str, Any]:
    return {
        "desktop_clipboard_read": clipboard_read,
        "desktop_clipboard_write": clipboard_write,
    }


__all__ = ["clipboard_read", "clipboard_write", "handlers"]
