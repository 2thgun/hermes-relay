"""Non-GUI tool handlers for the Python relay client.

Only tools this package can actually serve are registered, and the registry's
name list is what the client advertises in its ``desktop.status`` heartbeat.
Phase 1 deliberately excludes GUI/computer-use, browser/CDP, screenshot, USB,
ADB, and file-transfer tools; those land in later porting phases.
"""

from __future__ import annotations

from ..dispatch import ClientState, ToolRegistry
from . import clipboard, files, jobs, processes, shell
from .health import make_health_handler


def build_registry(
    *,
    state: ClientState,
    allow: set[str] | None = None,
    deny: set[str] | None = None,
) -> ToolRegistry:
    """Build the phase-1 tool registry.

    ``allow`` restricts the registry to the named tools (CLI ``--allow-tools``);
    ``deny`` removes names (CLI ``--deny-tools``).  Both filters also shape the
    advertised tool list, so the relay never routes an unavailable tool.
    """
    registry = ToolRegistry(allow=allow, deny=deny)
    registry.extend(files.handlers())
    registry.extend(shell.handlers())
    registry.extend(jobs.handlers())
    registry.extend(processes.handlers())
    registry.extend(clipboard.handlers())
    registry.add("desktop_health", make_health_handler(state, registry=registry))
    return registry


def all_tool_names() -> list[str]:
    """Every tool name the client can advertise on this platform."""
    state = ClientState()
    return build_registry(state=state).names()


__all__ = ["all_tool_names", "build_registry"]

# Re-export the handler modules for tests and future phases.
_ = (clipboard, files, jobs, processes, shell)
