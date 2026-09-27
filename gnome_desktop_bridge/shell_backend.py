"""Client for the optional GNOME Shell companion extension.

On Wayland an application does not know where the compositor placed its
windows, so AT-SPI "screen" coordinates of native Wayland clients are really
window-relative. The companion extension runs inside GNOME Shell, knows the
real window geometry, and exports a tiny D-Bus object on the Shell's own bus
name. This module asks it to translate window-relative coordinates.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

SHELL_BUS_NAME = "org.gnome.Shell"
OVERLAY_PATH = "/io/github/woodywizard/GnomeDesktopBridge"
OVERLAY_INTERFACE = "io.github.woodywizard.GnomeDesktopBridge.Overlay1"

# Do not retry an absent extension on every command.
UNAVAILABLE_RETRY_SECONDS = 5.0


def wayland_session() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE", "").casefold() == "wayland"
    )


class ShellBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: Any | None = None
        self._gio: Any | None = None
        self._glib: Any | None = None
        self._unavailable_until = 0.0

    def _ensure(self) -> bool:
        if self._connection is not None:
            return True
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib

            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            return False
        if connection is None:
            return False
        self._gio, self._glib, self._connection = Gio, GLib, connection
        return True

    def window_origin(
        self,
        pid: int | None,
        title: str,
        width: int,
        height: int,
    ) -> tuple[bool, int, int] | None:
        """Return (needs_offset, x, y) for the matching window, or None.

        needs_offset is False for XWayland clients, whose AT-SPI coordinates
        are already global.
        """

        with self._lock:
            if time.monotonic() < self._unavailable_until or not self._ensure():
                return None
            try:
                reply = self._connection.call_sync(
                    SHELL_BUS_NAME,
                    OVERLAY_PATH,
                    OVERLAY_INTERFACE,
                    "WindowOrigin",
                    self._glib.Variant(
                        "(usii)",
                        (max(0, int(pid or 0)), title[:1000], int(width), int(height)),
                    ),
                    self._glib.VariantType.new("(bbii)"),
                    self._gio.DBusCallFlags.NONE,
                    500,
                    None,
                )
            except Exception:
                self._unavailable_until = time.monotonic() + UNAVAILABLE_RETRY_SECONDS
                return None
            found, needs_offset, x, y = reply.unpack()
            if not found:
                return None
            return bool(needs_offset), int(x), int(y)
