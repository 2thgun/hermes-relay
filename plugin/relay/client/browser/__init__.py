"""Browser/CDP surface for the headless Python relay client.

The browser is a separate process launched with a dedicated automation profile
(``$HERMES_HOME/browser-profile``); this package never attaches to a signed-in
user profile.  Modules:

* :mod:`launcher`   binary discovery, dedicated-profile launch, DevTools endpoint
* :mod:`cdp`        minimal CDP client over aiohttp WebSocket
* :mod:`controller` inspect refs, click/type/key/scroll, screenshots, downloads

Unlike the desktop CLI, the Python client runs on headless hosts too: a browser
tool call does not require a graphical desktop session, and the launcher can run
Chromium with ``--headless=new`` (``HERMES_RELAY_BROWSER_HEADLESS=1``).  The
relay still enforces tool policy and computer-use grants before forwarding.
"""

from __future__ import annotations

from .cdp import (
    CdpConnection,
    CdpConnectionClosed,
    CdpConnectionError,
    CdpError,
    CdpEvent,
    CdpTimeout,
    open_cdp,
)
from .controller import BrowserController, BrowserError, ensure_allowed_url
from .launcher import (
    BrowserLauncher,
    BrowserLaunchError,
    BrowserLaunchResult,
    BrowserStatus,
)

__all__ = [
    "BrowserController",
    "BrowserError",
    "BrowserLaunchError",
    "BrowserLaunchResult",
    "BrowserLauncher",
    "BrowserStatus",
    "CdpConnection",
    "CdpConnectionClosed",
    "CdpConnectionError",
    "CdpError",
    "CdpEvent",
    "CdpTimeout",
    "ensure_allowed_url",
    "open_cdp",
]
