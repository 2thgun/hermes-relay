"""Process inspection/control handlers for the Python relay client.

Mirrors ``desktop/src/tools/handlers/process.ts``:

* ``desktop_list_processes``   — ``{processes, count, total, truncated}``
* ``desktop_kill_process``     — ``{killed, failed, requested}``
* ``desktop_find_pid_by_port`` — ``{port, protocol, listeners:[{pid,address}]}``

POSIX uses ``ps -eo pid,ppid,user,pcpu,rss,command``; Windows uses
``tasklist /FO CSV``.  Port lookups prefer ``ss`` on Linux, ``lsof`` on macOS,
and ``netstat -ano`` on Windows.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import signal
import subprocess
import sys
from typing import Any

from ..dispatch import ToolContext, ToolError

_PS_LINE = re.compile(r"^(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\d+)\s+(.*)$")


def _optional_str(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and value else None


def _arg_int(args: dict[str, Any], name: str, default: int, *, minimum: int = 0) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = int(value)
    return value if value >= minimum else default


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        raise ToolError(f"command not found: {argv[0]}") from None
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{argv[0]} timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise ToolError(str(exc)) from exc


# ── Parsers ─────────────────────────────────────────────────────────────────


def _parse_posix_ps(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in stdout.split("\n")[1:]:
        match = _PS_LINE.match(raw.strip())
        if not match:
            continue
        rows.append(
            {
                "pid": int(match.group(1)),
                "ppid": int(match.group(2)),
                "user": match.group(3),
                "cpu_pct": float(match.group(4)),
                "mem_mb": round(int(match.group(5)) / 1024),
                "command": match.group(6),
            }
        )
    return rows


def _parse_tasklist(stdout: str) -> list[dict[str, Any]]:
    reader = csv.reader(io.StringIO(stdout))
    rows: list[dict[str, Any]] = []
    next(reader, None)  # header
    for fields in reader:
        if len(fields) < 5:
            continue
        try:
            pid = int(fields[1])
        except (TypeError, ValueError):
            continue
        digits = re.sub(r"[^\d]", "", fields[4] or "")
        row: dict[str, Any] = {"pid": pid, "command": fields[0] or ""}
        if digits:
            row["mem_mb"] = round(int(digits) / 1024)
        rows.append(row)
    return rows


def _list_processes_sync() -> list[dict[str, Any]]:
    if sys.platform == "win32":
        result = _run(["tasklist", "/FO", "CSV"])
        if result.returncode != 0:
            raise ToolError(result.stderr.strip() or "tasklist failed")
        return _parse_tasklist(result.stdout)
    result = _run(["ps", "-eo", "pid,ppid,user,pcpu,rss,command"])
    if result.returncode != 0:
        raise ToolError(result.stderr.strip() or "ps failed")
    return _parse_posix_ps(result.stdout)


# ── desktop_list_processes ─────────────────────────────────────────────────


async def list_processes(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    needle = (_optional_str(args, "filter") or "").lower()
    limit = _arg_int(args, "limit", 200, minimum=1)
    processes = await asyncio.to_thread(_list_processes_sync)
    filtered = (
        [row for row in processes if needle in str(row.get("command", "")).lower()]
        if needle
        else processes
    )
    selected = filtered[:limit]
    return {
        "processes": selected,
        "count": len(selected),
        "total": len(filtered),
        "truncated": len(filtered) > limit,
    }


# ── desktop_kill_process ───────────────────────────────────────────────────


def _taskkill(pid: int, force: bool) -> tuple[bool, str]:
    argv = ["taskkill", "/PID", str(pid)]
    if force:
        argv.insert(1, "/F")
    result = _run(argv)
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


async def kill_process(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_pid = args.get("pid")
    pid = (
        int(raw_pid)
        if isinstance(raw_pid, (int, float)) and not isinstance(raw_pid, bool)
        else None
    )
    name = _optional_str(args, "name")
    force = args.get("force") is True
    signal_arg = (_optional_str(args, "signal") or "TERM").upper()

    if pid is None and name is None:
        raise ToolError('desktop_kill_process requires either "pid" or "name"')

    if pid is not None:
        targets = [pid]
    else:
        listed = await asyncio.to_thread(_list_processes_sync)
        targets = [
            row["pid"]
            for row in listed
            if name is not None and name.lower() in str(row.get("command", "")).lower()
        ][:100]

    killed: list[int] = []
    failed: list[dict[str, Any]] = []
    for target in targets:
        try:
            if sys.platform == "win32":
                ok, detail = await asyncio.to_thread(_taskkill, target, force)
                if ok:
                    killed.append(target)
                else:
                    failed.append({"pid": target, "error": detail or "taskkill failed"})
            else:
                sig = signal.SIGKILL if force or signal_arg == "KILL" else signal.SIGTERM
                os.kill(target, sig)
                killed.append(target)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            failed.append({"pid": target, "error": str(exc)})
    return {"killed": killed, "failed": failed, "requested": len(targets)}


# ── desktop_find_pid_by_port ────────────────────────────────────────────────


def _listeners_from_netstat(stdout: str, port: int, protocol: str) -> list[dict[str, Any]]:
    want = "TCP" if protocol == "tcp" else "UDP"
    listeners: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.upper().startswith(want):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[1]
        if ":" not in local:
            continue
        try:
            if int(local.rsplit(":", 1)[1]) != port:
                continue
            pid = int(fields[-1])
        except ValueError:
            continue
        if protocol == "tcp" and "LISTENING" not in fields:
            continue
        listeners.append({"pid": pid, "address": local})
    return listeners


def _listeners_from_lsof(stdout: str) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for raw in stdout.split("\n")[1:]:
        fields = raw.split()
        if len(fields) < 9:
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        listeners.append({"pid": pid, "address": fields[8]})
    return listeners


def _listeners_from_ss(stdout: str, port: int) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for raw in stdout.split("\n")[1:]:
        line = raw.strip()
        if not line:
            continue
        port_match = re.search(r":(\d+)\s", line)
        if not port_match or int(port_match.group(1)) != port:
            continue
        fields = line.split()
        address = fields[3] if len(fields) > 3 else ""
        for match in re.finditer(r"pid=(\d+)", line):
            listeners.append({"pid": int(match.group(1)), "address": address})
    return listeners


def _dedupe(listeners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[int, str]] = set()
    out: list[dict[str, Any]] = []
    for row in listeners:
        key = (row["pid"], row["address"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _find_pid_by_port_sync(port: int, protocol: str) -> list[dict[str, Any]]:
    if sys.platform == "win32":
        result = _run(["netstat", "-ano"])
        return _listeners_from_netstat(result.stdout, port, protocol)

    if sys.platform == "darwin":
        args = ["-nP", f"-i{'TCP' if protocol == 'tcp' else 'UDP'}:{port}"]
        if protocol == "tcp":
            args.append("-sTCP:LISTEN")
        result = _run(["lsof", *args])
        return _listeners_from_lsof(result.stdout)

    try:
        result = _run(["ss", "-lntp" if protocol == "tcp" else "-lnup"])
    except ToolError:
        result = None
    if result is not None and result.returncode == 0:
        return _listeners_from_ss(result.stdout, port)

    args = ["-nP", f"-i{'TCP' if protocol == 'tcp' else 'UDP'}:{port}"]
    if protocol == "tcp":
        args.append("-sTCP:LISTEN")
    fallback = _run(["lsof", *args])
    return _listeners_from_lsof(fallback.stdout)


async def find_pid_by_port(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_port = args.get("port")
    if isinstance(raw_port, bool) or not isinstance(raw_port, (int, float)) or raw_port < 0:
        raise ToolError('"port" must be a non-negative number')
    port = int(raw_port)
    protocol = (_optional_str(args, "protocol") or "tcp").lower()
    if protocol not in {"tcp", "udp"}:
        raise ToolError('"protocol" must be "tcp" or "udp"')
    listeners = await asyncio.to_thread(_find_pid_by_port_sync, port, protocol)
    return {"port": port, "protocol": protocol, "listeners": _dedupe(listeners)}


def handlers() -> dict[str, Any]:
    return {
        "desktop_list_processes": list_processes,
        "desktop_kill_process": kill_process,
        "desktop_find_pid_by_port": find_pid_by_port,
    }


__all__ = ["find_pid_by_port", "handlers", "kill_process", "list_processes"]
