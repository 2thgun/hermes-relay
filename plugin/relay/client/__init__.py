"""Headless Python client for Hermes-Relay (phase 1).

This package is the controlled-computer side of the relay protocol.  It pairs
with a relay over WebSocket, stores the minted session token under
``$HERMES_HOME/relay/client.json``, and serves the non-GUI ``desktop_*`` tools:

``desktop_read_file``, ``desktop_write_file``, ``desktop_patch``,
``desktop_search_files``, ``desktop_checksum``, ``desktop_terminal``,
``desktop_powershell`` (Windows), ``desktop_job_start/status/logs/cancel/list``,
``desktop_list_processes``, ``desktop_kill_process``,
``desktop_find_pid_by_port``, ``desktop_clipboard_read``,
``desktop_clipboard_write``, and ``desktop_health``.

CLI::

    python -m plugin.relay.client pair --url wss://host:8767 --code ABC123
    python -m plugin.relay.client run  --url wss://host:8767

GUI, browser/CDP, computer-use, USB/ADB, screenshot, and file-transfer tools
are intentionally out of scope for phase 1.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
