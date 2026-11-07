"""Filesystem tool handlers for the Python relay client.

Wire-compatible with ``desktop/src/tools/handlers/fs.ts``,
``search.ts``, and the checksum half of ``transfer.ts``:

* ``desktop_read_file``    — bounded UTF-8 read with a truncation marker
* ``desktop_write_file``   — write text, optionally creating parent dirs
* ``desktop_patch``        — strict unified-diff application (no fuzz)
* ``desktop_search_files`` — ripgrep-backed content search, glob path search
* ``desktop_checksum``     — streaming sha256/sha1/md5

Paths are resolved against the client's working directory (``--cwd``), with
absolute paths passing through unchanged.  Errors are raised as ``ToolError``
and serialized by the dispatcher into ``{ok: false, error}``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from ..dispatch import ToolContext, ToolError

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_SEARCH_RESULTS = 100
SKIP_DIRS = frozenset({".git", "node_modules", "dist", ".next", ".cache"})
_FALLBACK_MAX_FILE_BYTES = 5_000_000
_CHECKSUM_CHUNK = 1024 * 1024


# ── Argument helpers ────────────────────────────────────────────────────────


def _require_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or value == "":
        raise ToolError(f'missing or invalid "{name}" argument')
    return value


def _optional_str(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and value else None


def _arg_int(args: dict[str, Any], name: str, default: int, *, minimum: int = 0) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = int(value)
    if value < minimum:
        return default
    return value


def _arg_bool(args: dict[str, Any], name: str, default: bool) -> bool:
    value = args.get(name)
    return value if isinstance(value, bool) else default


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(ctx.cwd) / path
    return path.resolve()


# ── desktop_read_file ───────────────────────────────────────────────────────


async def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    """Read a file as UTF-8 text, truncating at ``max_bytes``.

    Returns a plain string (matching the TypeScript handler).  Truncation
    appends ``\\n[... truncated at N bytes]`` so the agent can detect it.
    """
    path = resolve_path(ctx, _require_str(args, "path"))
    max_bytes = _arg_int(args, "max_bytes", DEFAULT_MAX_BYTES)
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise ToolError(f"ENOENT: no such file or directory, stat '{path}'") from exc
    except PermissionError as exc:
        raise ToolError(f"EACCES: permission denied, open '{path}'") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc

    if not path.is_file():
        raise ToolError(f"not a regular file: {path}")

    try:
        if stat.st_size <= max_bytes:
            return path.read_bytes().decode("utf-8", errors="replace")
        with path.open("rb") as handle:
            chunk = handle.read(max_bytes)
        return chunk.decode("utf-8", errors="replace") + f"\n[... truncated at {max_bytes} bytes]"
    except PermissionError as exc:
        raise ToolError(f"EACCES: permission denied, open '{path}'") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc


# ── desktop_write_file ──────────────────────────────────────────────────────


async def write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Write UTF-8 text, optionally creating parent directories."""
    path = resolve_path(ctx, _require_str(args, "path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError('missing or invalid "content" argument')
    create_dirs = _arg_bool(args, "create_dirs", False)

    try:
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        # Write bytes so Windows does not translate "\n" to "\r\n" (Node's
        # fs.writeFile with utf8 writes the string verbatim).
        path.write_bytes(content.encode("utf-8"))
    except PermissionError as exc:
        raise ToolError(f"EACCES: permission denied, open '{path}'") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc

    return {
        "ok": True,
        "bytes_written": len(content.encode("utf-8")),
        "path": str(path),
    }


# ── desktop_patch ───────────────────────────────────────────────────────────
#
# Deliberately strict: no fuzz, no context shifting.  If a hunk's pre-image
# does not match the file at the claimed line range, the whole patch is
# rejected rather than silently corrupting the file.

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class _Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines")

    def __init__(
        self,
        old_start: int,
        old_len: int,
        new_start: int,
        new_len: int,
        lines: list[str],
    ) -> None:
        self.old_start = old_start
        self.old_len = old_len
        self.new_start = new_start
        self.new_len = new_len
        self.lines = lines


def _parse_unified_diff(patch: str) -> list[_Hunk]:
    lines = patch.split("\n")
    # A patch ending with a newline produces one trailing empty element; it is
    # not a context line and would add a phantom line to the pre-image.
    if lines and lines[-1] == "":
        lines.pop()

    hunks: list[_Hunk] = []
    index = 0
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if not match:
            index += 1
            continue

        old_start = int(match.group(1))
        old_len = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_len = int(match.group(4)) if match.group(4) is not None else 1
        index += 1
        body: list[str] = []

        while index < len(lines):
            line = lines[index]
            if _HUNK_HEADER.match(line):
                break
            if line.startswith("--- ") or line.startswith("+++ "):
                break
            if line == "":
                body.append(" ")
                index += 1
                continue
            prefix = line[0]
            if prefix not in {" ", "+", "-", "\\"}:
                break
            if prefix == "\\":
                # "\ No newline at end of file" — trailing-newline state is
                # handled at write time.
                index += 1
                continue
            body.append(line)
            index += 1

        hunks.append(_Hunk(old_start, old_len, new_start, new_len, body))

    if not hunks:
        raise ToolError("no hunks found in patch")
    return hunks


def _apply_hunk(source: list[str], hunk: _Hunk) -> list[str]:
    pre: list[str] = []
    post: list[str] = []
    for line in hunk.lines:
        tag, content = line[0], line[1:]
        if tag == " ":
            pre.append(content)
            post.append(content)
        elif tag == "-":
            pre.append(content)
        elif tag == "+":
            post.append(content)

    start = hunk.old_start - 1
    if start < 0 or start + len(pre) > len(source):
        raise ToolError(
            f"hunk @@ -{hunk.old_start},{hunk.old_len} @@ out of range "
            f"(file has {len(source)} lines)"
        )
    for offset, expected in enumerate(pre):
        if source[start + offset] != expected:
            raise ToolError(
                f"hunk @@ -{hunk.old_start},{hunk.old_len} @@ context mismatch "
                f"at line {start + offset + 1}"
            )
    return source[:start] + post + source[start + len(pre):]


async def patch_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Apply a unified diff to one file.  Any mismatch aborts the whole patch."""
    path = resolve_path(ctx, _require_str(args, "path"))
    patch_text = _require_str(args, "patch")

    try:
        hunks = _parse_unified_diff(patch_text)
    except ToolError as exc:
        raise ToolError(f"patch did not apply cleanly: {exc}") from exc

    try:
        original = path.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise ToolError(f"ENOENT: no such file or directory, open '{path}'") from exc
    except UnicodeDecodeError as exc:
        raise ToolError(f"file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc

    had_trailing_newline = original.endswith("\n")
    source = original.split("\n")
    if had_trailing_newline:
        source.pop()

    working = source
    for hunk in sorted(hunks, key=lambda item: item.old_start, reverse=True):
        try:
            working = _apply_hunk(working, hunk)
        except ToolError as exc:
            raise ToolError(f"patch did not apply cleanly: {exc}") from exc

    joined = "\n".join(working) + ("\n" if had_trailing_newline else "")
    try:
        path.write_bytes(joined.encode("utf-8"))
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "hunks_applied": len(hunks), "path": str(path), "approved": "auto"}


# ── desktop_search_files ────────────────────────────────────────────────────


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate ``*`` / ``?`` / ``**`` into a full-path regular expression."""
    out = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                out.append(".*")
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char in ".+^$()|{}[]\\":
            out.append("\\" + char)
            index += 1
        else:
            out.append(char)
            index += 1
    out.append("$")
    return re.compile("".join(out))


def _walk_files(root: Path) -> list[Path]:
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            collected.append(Path(dirpath) / name)
    return collected


def _path_search(pattern: str, root: Path, maximum: int) -> tuple[list[str], bool]:
    regex = _glob_to_regex(pattern.replace("\\", "/"))
    matches: list[str] = []
    for file in _walk_files(root):
        try:
            relative = file.relative_to(root).as_posix()
        except ValueError:
            relative = file.as_posix()
        if regex.match(relative) or regex.match(file.name):
            matches.append(str(file))
            if len(matches) >= maximum:
                return matches, True
    return matches, False


async def _run_rg(pattern: str, root: Path, maximum: int) -> tuple[list[str], bool] | None:
    """Return matches, or ``None`` when ripgrep is not installed."""
    args = ["--files-with-matches", "--no-messages", "--max-count", "1"]
    for skipped in sorted(SKIP_DIRS):
        args.extend(["--glob", f"!{skipped}"])
    args.extend(["-e", pattern, str(root)])
    try:
        process = await asyncio.create_subprocess_exec(
            "rg",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ToolError(f"failed to start ripgrep: {exc}") from exc

    stdout, stderr = await process.communicate()
    if process.returncode not in (0, 1):
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"ripgrep failed: {detail or f'exit code {process.returncode}'}")
    lines = [line for line in stdout.decode("utf-8", errors="replace").split("\n") if line]
    return lines[:maximum], len(lines) > maximum


def _fallback_content_search(pattern: str, root: Path, maximum: int) -> tuple[list[str], bool]:
    needle = pattern.encode("utf-8")
    matches: list[str] = []
    for file in _walk_files(root):
        try:
            if file.stat().st_size > _FALLBACK_MAX_FILE_BYTES:
                continue
            if needle in file.read_bytes():
                matches.append(str(file))
                if len(matches) >= maximum:
                    return matches, True
        except OSError:
            continue
    return matches, False


async def search_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search file paths (glob) or contents (regex via ripgrep)."""
    pattern = _require_str(args, "pattern")
    raw_cwd = _optional_str(args, "cwd")
    root = resolve_path(ctx, raw_cwd) if raw_cwd else Path(ctx.cwd).resolve()
    maximum = max(1, _arg_int(args, "max_results", DEFAULT_SEARCH_RESULTS, minimum=1))
    content = _arg_bool(args, "content", False)

    if not root.is_dir():
        raise ToolError(f"not a directory: {root}")

    if content:
        result = await _run_rg(pattern, root, maximum)
        if result is not None:
            matches, truncated = result
            return {"matches": matches, "truncated": truncated}
        matches, truncated = _fallback_content_search(pattern, root, maximum)
        return {"matches": matches, "truncated": truncated}

    matches, truncated = _path_search(pattern, root, maximum)
    return {"matches": matches, "truncated": truncated}


# ── desktop_checksum ────────────────────────────────────────────────────────


async def checksum(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Stream a digest over a file (sha256 / sha1 / md5)."""
    path = resolve_path(ctx, _require_str(args, "path"))
    algorithm = (_optional_str(args, "algorithm") or "sha256").lower()
    if algorithm not in {"sha256", "sha1", "md5"}:
        raise ToolError('"algorithm" must be sha256, sha1, or md5')
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise ToolError(f"ENOENT: no such file or directory, stat '{path}'") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    if not path.is_file():
        raise ToolError(f"not a regular file: {path}")

    digest = hashlib.new(algorithm)
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHECKSUM_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ToolError(str(exc)) from exc

    return {
        "path": str(path),
        "algorithm": algorithm,
        "hash": digest.hexdigest(),
        "size_bytes": stat.st_size,
    }


def handlers() -> dict[str, Any]:
    """Handler map for the filesystem tool family."""
    return {
        "desktop_read_file": read_file,
        "desktop_write_file": write_file,
        "desktop_patch": patch_file,
        "desktop_search_files": search_files,
        "desktop_checksum": checksum,
    }


__all__ = [
    "checksum",
    "handlers",
    "patch_file",
    "read_file",
    "resolve_path",
    "search_files",
    "write_file",
]
