"""GNOME/XDG portal integration for screenshots and consented input control."""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import AppPaths, _atomic_write
from .errors import BackendUnavailable, BridgeError, InvalidRequest

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
REMOTE_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
SCREENSHOT_INTERFACE = "org.freedesktop.portal.Screenshot"

DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
SOURCE_MONITOR = 1
CURSOR_EMBEDDED = 2
KEY_RELEASED = 0
KEY_PRESSED = 1

BUTTON_CODES = {
    "left": 0x110,
    "right": 0x111,
    "middle": 0x112,
    "side": 0x113,
    "extra": 0x114,
}


@dataclass(frozen=True, slots=True)
class PortalStream:
    node_id: int
    x: int = 0
    y: int = 0
    width: int | None = None
    height: int | None = None
    mapping_id: str = ""

    def contains(self, x: float, y: float) -> bool:
        if self.width is None or self.height is None:
            return True
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "position": {"x": self.x, "y": self.y},
            "size": (
                {"width": self.width, "height": self.height}
                if self.width is not None and self.height is not None
                else None
            ),
            "mappingId": self.mapping_id or None,
        }


class PortalBackend:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.discover()
        self.paths.ensure()
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._input_cancel = threading.Event()
        self._cancel_serial = 0
        self._active_request_path: str | None = None
        self._pending_session_handle: str | None = None
        self._remote_starting = False
        self._gio: Any | None = None
        self._glib: Any | None = None
        self._connection: Any | None = None
        self._loop: Any | None = None
        self._loop_thread: threading.Thread | None = None
        self._session_handle: str | None = None
        self._session_signal_id: int | None = None
        self._devices = 0
        self._streams: list[PortalStream] = []
        self._restore_token_file = self.paths.data_dir / "portal-restore-token"

    def _ensure(self) -> tuple[Any, Any, Any]:
        with self._lock:
            if self._connection is not None:
                return self._gio, self._glib, self._connection
            try:
                import gi

                gi.require_version("Gio", "2.0")
                gi.require_version("GLib", "2.0")
                from gi.repository import Gio, GLib

                connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                if connection is None:
                    raise RuntimeError("No session D-Bus connection")
                loop = GLib.MainLoop()
                loop_thread = threading.Thread(
                    target=loop.run,
                    name="gnome-desktop-bridge-glib",
                    daemon=True,
                )
                loop_thread.start()
            except Exception as exc:
                raise BackendUnavailable(
                    "portal",
                    "Cannot connect to the GNOME desktop portal in this user session.",
                ) from exc
            self._gio = Gio
            self._glib = GLib
            self._connection = connection
            self._loop = loop
            self._loop_thread = loop_thread
            return Gio, GLib, connection

    def available(self) -> tuple[bool, str | None]:
        try:
            self._ensure()
            self._get_property(SCREENSHOT_INTERFACE, "version")
            return True, None
        except (BackendUnavailable, BridgeError) as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._session_handle is not None

    @property
    def control_in_progress(self) -> bool:
        with self._lock:
            return self._session_handle is not None or self._remote_starting

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._session_handle is not None,
                "starting": self._remote_starting and self._session_handle is None,
                "devices": {
                    "keyboard": bool(self._devices & DEVICE_KEYBOARD),
                    "pointer": bool(self._devices & DEVICE_POINTER),
                },
                "streams": [stream.as_dict() for stream in self._streams],
            }

    def versions(self) -> dict[str, int | None]:
        result: dict[str, int | None] = {}
        for name, interface in {
            "remoteDesktop": REMOTE_INTERFACE,
            "screenCast": SCREENCAST_INTERFACE,
            "screenshot": SCREENSHOT_INTERFACE,
        }.items():
            try:
                result[name] = int(self._get_property(interface, "version"))
            except Exception:
                result[name] = None
        return result

    def _get_property(self, interface: str, name: str) -> Any:
        Gio, GLib, connection = self._ensure()
        try:
            reply = connection.call_sync(
                PORTAL_NAME,
                PORTAL_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (interface, name)),
                GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            return reply.unpack()[0]
        except Exception as exc:
            raise BackendUnavailable(
                "portal", f"Portal interface unavailable: {interface}"
            ) from exc

    def _request(
        self,
        interface: str,
        method: str,
        signature: str,
        values_factory: Any,
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Call a portal request method and wait for its Response signal.

        values_factory receives the generated handle token so the signal can be
        subscribed before the method call, avoiding the standard portal race.
        """

        Gio, GLib, connection = self._ensure()
        with self._request_lock:
            unique_name = connection.get_unique_name() or "unknown"
            sender = unique_name.lstrip(":").replace(".", "_")
            handle_token = f"gdb_{secrets.token_hex(12)}"
            predicted_path = f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"
            completed = threading.Event()
            response: dict[str, Any] = {}

            def on_response(
                _connection: Any,
                _sender_name: str,
                _object_path: str,
                _interface_name: str,
                _signal_name: str,
                parameters: Any,
                _user_data: Any,
            ) -> None:
                try:
                    response_code, results = parameters.unpack()
                    response["code"] = int(response_code)
                    response["results"] = results
                except Exception as exc:  # pragma: no cover - defensive around D-Bus bindings
                    response["exception"] = exc
                finally:
                    completed.set()

            subscription_id = connection.signal_subscribe(
                PORTAL_NAME,
                REQUEST_INTERFACE,
                "Response",
                predicted_path,
                None,
                Gio.DBusSignalFlags.NONE,
                on_response,
                None,
            )
            actual_path = predicted_path
            with self._lock:
                self._active_request_path = predicted_path
            try:
                values = values_factory(handle_token)
                parameters = GLib.Variant(signature, values)
                reply = connection.call_sync(
                    PORTAL_NAME,
                    PORTAL_PATH,
                    interface,
                    method,
                    parameters,
                    GLib.VariantType.new("(o)"),
                    Gio.DBusCallFlags.NONE,
                    30_000,
                    None,
                )
                actual_path = str(reply.unpack()[0])
                if actual_path != predicted_path:
                    # Modern portals honor handle_token. Treat a mismatch as a
                    # compatibility error rather than risking a lost response.
                    raise BackendUnavailable(
                        "portal",
                        "The portal returned an unexpected request handle.",
                    )
                if not completed.wait(timeout):
                    self._close_request(actual_path)
                    raise BackendUnavailable(
                        "portal",
                        f"Timed out waiting for portal response to {method}",
                    )
                if "exception" in response:
                    raise BackendUnavailable("portal", "Cannot decode the portal response")
                response_code = response.get("code", 2)
                if response_code == 1:
                    raise BridgeError(
                        "portal_cancelled",
                        "The desktop permission dialog was cancelled.",
                        409,
                    )
                if response_code != 0:
                    raise BridgeError(
                        "portal_denied",
                        "The desktop portal did not grant the requested operation.",
                        403,
                        {"portalResponse": response_code},
                    )
                results = response.get("results", {})
                return results if isinstance(results, dict) else {}
            except BridgeError:
                raise
            except Exception as exc:
                raise BackendUnavailable("portal", f"Portal call {method} failed: {exc}") from exc
            finally:
                with self._lock:
                    if self._active_request_path == predicted_path:
                        self._active_request_path = None
                connection.signal_unsubscribe(subscription_id)

    def _close_request(self, request_path: str) -> None:
        Gio, _GLib, connection = self._ensure()
        try:
            connection.call_sync(
                PORTAL_NAME,
                request_path,
                REQUEST_INTERFACE,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception:
            pass

    def screenshot(
        self, *, interactive: bool = False, include_base64: bool = False
    ) -> dict[str, Any]:
        def values(handle_token: str) -> tuple[str, dict[str, Any]]:
            options = {
                "handle_token": self._glib.Variant("s", handle_token),
                "interactive": self._glib.Variant("b", bool(interactive)),
            }
            return "", options

        results = self._request(SCREENSHOT_INTERFACE, "Screenshot", "(sa{sv})", values)
        uri = results.get("uri")
        if not isinstance(uri, str):
            raise BackendUnavailable("portal", "The screenshot portal returned no file URI")
        source = self._path_from_file_uri(uri)
        if not source.is_file():
            raise BackendUnavailable("portal", "The screenshot file is unavailable")
        if source.stat().st_size > 200 * 1024 * 1024:
            raise BackendUnavailable("portal", "The screenshot exceeds the 200 MiB safety limit")

        self.paths.screenshots_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.paths.screenshots_dir.chmod(0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = self.paths.screenshots_dir / f"screenshot-{stamp}-{secrets.token_hex(4)}.png"
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        payload: dict[str, Any] = {
            "path": str(destination),
            "size": destination.stat().st_size,
            "mimeType": "image/png",
        }
        if include_base64:
            if payload["size"] > 25 * 1024 * 1024:
                raise InvalidRequest("Screenshot is too large to return as base64")
            payload["base64"] = base64.b64encode(destination.read_bytes()).decode("ascii")
        return payload

    @staticmethod
    def _path_from_file_uri(uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise BackendUnavailable("portal", "Portal returned a non-local screenshot URI")
        return Path(unquote(parsed.path))

    def start_remote_desktop(self, *, persist: bool = False) -> dict[str, Any]:
        with self._start_lock:
            with self._lock:
                if self._session_handle is not None:
                    return self.status()
                cancel_serial = self._cancel_serial
                self._remote_starting = True
            restore_token = self._load_restore_token() if persist else None
            session_handle: str | None = None
            try:
                create_results = self._request(
                    REMOTE_INTERFACE,
                    "CreateSession",
                    "(a{sv})",
                    lambda handle_token: (
                        {
                            "handle_token": self._glib.Variant("s", handle_token),
                            "session_handle_token": self._glib.Variant(
                                "s", f"gdb_session_{secrets.token_hex(10)}"
                            ),
                        },
                    ),
                )
                session_handle = create_results.get("session_handle")
                if not isinstance(session_handle, str):
                    raise BackendUnavailable("portal", "Portal returned no desktop session handle")
                with self._lock:
                    self._pending_session_handle = session_handle
                self._check_not_cancelled(cancel_serial)

                def device_values(handle_token: str) -> tuple[str, dict[str, Any]]:
                    options: dict[str, Any] = {
                        "handle_token": self._glib.Variant("s", handle_token),
                        "types": self._glib.Variant("u", DEVICE_KEYBOARD | DEVICE_POINTER),
                    }
                    if persist:
                        options["persist_mode"] = self._glib.Variant("u", 2)
                        if restore_token:
                            options["restore_token"] = self._glib.Variant("s", restore_token)
                    return session_handle, options

                self._request(
                    REMOTE_INTERFACE,
                    "SelectDevices",
                    "(oa{sv})",
                    device_values,
                )
                self._check_not_cancelled(cancel_serial)

                def source_values(handle_token: str) -> tuple[str, dict[str, Any]]:
                    options = {
                        "handle_token": self._glib.Variant("s", handle_token),
                        "types": self._glib.Variant("u", SOURCE_MONITOR),
                        "multiple": self._glib.Variant("b", False),
                        "cursor_mode": self._glib.Variant("u", CURSOR_EMBEDDED),
                    }
                    return session_handle, options

                self._request(
                    SCREENCAST_INTERFACE,
                    "SelectSources",
                    "(oa{sv})",
                    source_values,
                )
                self._check_not_cancelled(cancel_serial)

                start_results = self._request(
                    REMOTE_INTERFACE,
                    "Start",
                    "(osa{sv})",
                    lambda handle_token: (
                        session_handle,
                        "",
                        {"handle_token": self._glib.Variant("s", handle_token)},
                    ),
                )
                self._check_not_cancelled(cancel_serial)
                returned_restore_token = start_results.get("restore_token")
                with self._lock:
                    self._check_not_cancelled(cancel_serial)
                    self._session_handle = session_handle
                    self._pending_session_handle = None
                    self._devices = int(
                        start_results.get("devices", DEVICE_KEYBOARD | DEVICE_POINTER)
                    )
                    self._streams = self._parse_streams(start_results.get("streams", []))
                    self._input_cancel.clear()
                    self._subscribe_session_closed(session_handle)
                    result = self.status()
                if persist and isinstance(returned_restore_token, str):
                    _atomic_write(self._restore_token_file, returned_restore_token + "\n")
                return result
            except Exception:
                if session_handle:
                    self._close_session_handle(session_handle)
                with self._lock:
                    if self._pending_session_handle == session_handle:
                        self._pending_session_handle = None
                    if self._session_handle == session_handle:
                        self._clear_session()
                raise
            finally:
                with self._lock:
                    self._remote_starting = False

    def _check_not_cancelled(self, serial: int) -> None:
        with self._lock:
            if serial != self._cancel_serial:
                raise BridgeError(
                    "operation_cancelled",
                    "The pending desktop portal operation was stopped.",
                    409,
                )

    def _parse_streams(self, raw_streams: Any) -> list[PortalStream]:
        streams: list[PortalStream] = []
        if not isinstance(raw_streams, (list, tuple)):
            return streams
        for item in raw_streams:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            node_id, properties = item
            if not isinstance(properties, dict):
                properties = {}
            position = properties.get("position", (0, 0))
            size = properties.get("size")
            try:
                x, y = int(position[0]), int(position[1])
            except (TypeError, ValueError, IndexError):
                x, y = 0, 0
            width: int | None = None
            height: int | None = None
            try:
                if size is not None:
                    width, height = int(size[0]), int(size[1])
            except (TypeError, ValueError, IndexError):
                width, height = None, None
            try:
                streams.append(
                    PortalStream(
                        node_id=int(node_id),
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        mapping_id=str(properties.get("mapping_id", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return streams

    def _load_restore_token(self) -> str | None:
        fd = -1
        try:
            metadata = self._restore_token_file.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_size > 64 * 1024
            ):
                return None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._restore_token_file, flags)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                return None
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                token = handle.read(64 * 1024 + 1).strip()
            return token if token and len(token.encode("utf-8")) <= 64 * 1024 else None
        except (OSError, UnicodeError):
            return None
        finally:
            if fd >= 0:
                os.close(fd)

    def _subscribe_session_closed(self, session_handle: str) -> None:
        Gio, _GLib, connection = self._ensure()

        def closed(*_args: Any) -> None:
            with self._lock:
                if self._session_handle == session_handle:
                    self._clear_session()

        self._session_signal_id = connection.signal_subscribe(
            PORTAL_NAME,
            SESSION_INTERFACE,
            "Closed",
            session_handle,
            None,
            Gio.DBusSignalFlags.NONE,
            closed,
            None,
        )

    def _clear_session(self) -> None:
        if self._session_signal_id is not None and self._connection is not None:
            try:
                self._connection.signal_unsubscribe(self._session_signal_id)
            except Exception:
                pass
        self._session_signal_id = None
        self._session_handle = None
        self._pending_session_handle = None
        self._devices = 0
        self._streams = []
        self._input_cancel.set()

    def _close_session_handle(self, session_handle: str) -> None:
        Gio, _GLib, connection = self._ensure()
        try:
            connection.call_sync(
                PORTAL_NAME,
                session_handle,
                SESSION_INTERFACE,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception:
            pass

    def stop_remote_desktop(self) -> dict[str, Any]:
        # Set cancellation before waiting for any lock held by a short input
        # operation. Long text entry and an in-flight portal dialog can stop now.
        self._input_cancel.set()
        with self._lock:
            self._cancel_serial += 1
            request_path = self._active_request_path
            handles = {
                handle
                for handle in (self._session_handle, self._pending_session_handle)
                if handle is not None
            }
        if request_path is not None:
            self._close_request(request_path)
        for handle in handles:
            self._close_session_handle(handle)
        with self._lock:
            self._clear_session()
            return self.status()

    def _require_session(self, device: int) -> str:
        if self._session_handle is None:
            raise InvalidRequest("No active RemoteDesktop portal session")
        if not self._devices & device:
            label = "keyboard" if device == DEVICE_KEYBOARD else "pointer"
            raise BridgeError(
                "device_not_granted",
                f"The portal session did not grant {label} control.",
                403,
            )
        return self._session_handle

    def _notify(self, method: str, signature: str, values: tuple[Any, ...]) -> None:
        Gio, GLib, connection = self._ensure()
        try:
            connection.call_sync(
                PORTAL_NAME,
                PORTAL_PATH,
                REMOTE_INTERFACE,
                method,
                GLib.Variant(signature, values),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:
            raise BackendUnavailable("portal", f"Remote input call {method} failed: {exc}") from exc

    def _stream_for_point(self, x: float, y: float) -> PortalStream:
        if not self._streams:
            raise InvalidRequest(
                "The portal session has no screen stream; absolute pointer input is unavailable"
            )
        for stream in self._streams:
            if stream.contains(x, y):
                return stream
        raise InvalidRequest(
            "The point is outside the shared screen",
            details={"x": x, "y": y, "streams": [item.as_dict() for item in self._streams]},
        )

    def pointer_move(self, x: float, y: float) -> dict[str, Any]:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise InvalidRequest("x and y must be numbers")
        with self._lock:
            handle = self._require_session(DEVICE_POINTER)
            stream = self._stream_for_point(float(x), float(y))
            relative_x = float(x) - stream.x
            relative_y = float(y) - stream.y
            self._notify(
                "NotifyPointerMotionAbsolute",
                "(oa{sv}udd)",
                (handle, {}, stream.node_id, relative_x, relative_y),
            )
            return {"performed": True, "x": x, "y": y, "streamNodeId": stream.node_id}

    def pointer_click(
        self,
        *,
        button: str | int = "left",
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        if (x is None) != (y is None):
            raise InvalidRequest("x and y must be provided together")
        if isinstance(button, str):
            button_code = BUTTON_CODES.get(button.casefold())
        elif type(button) is int:
            button_code = button
        else:
            button_code = None
        if button_code is None or not 0 <= int(button_code) <= 0x7FFFFFFF:
            raise InvalidRequest("Unknown pointer button")
        with self._lock:
            handle = self._require_session(DEVICE_POINTER)
            if x is not None and y is not None:
                self.pointer_move(x, y)
            self._notify(
                "NotifyPointerButton",
                "(oa{sv}iu)",
                (handle, {}, int(button_code), KEY_PRESSED),
            )
            self._notify(
                "NotifyPointerButton",
                "(oa{sv}iu)",
                (handle, {}, int(button_code), KEY_RELEASED),
            )
            return {
                "performed": True,
                "button": button,
                "buttonCode": int(button_code),
                "x": x,
                "y": y,
            }

    def scroll(self, dx: float = 0.0, dy: float = 0.0) -> dict[str, Any]:
        if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
            raise InvalidRequest("dx and dy must be numbers")
        if abs(float(dx)) > 10_000 or abs(float(dy)) > 10_000:
            raise InvalidRequest("Scroll delta is too large")
        with self._lock:
            handle = self._require_session(DEVICE_POINTER)
            self._notify(
                "NotifyPointerAxis",
                "(oa{sv}dd)",
                (handle, {}, float(dx), float(dy)),
            )
            return {"performed": True, "dx": dx, "dy": dy}

    def _keyval(self, key: str | int) -> int:
        if type(key) is int:
            if 0 <= key <= 0x7FFFFFFF:
                return key
            raise InvalidRequest("Numeric keysym is outside the supported range")
        if not isinstance(key, str) or not key:
            raise InvalidRequest("key must be a keysym name, one character, or an integer")
        if len(key) > 100:
            raise InvalidRequest("key name is too long")
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            if len(key) == 1:
                value = int(Gdk.unicode_to_keyval(ord(key)))
            else:
                value = int(Gdk.keyval_from_name(key))
        except Exception as exc:
            raise BackendUnavailable("gdk", "Cannot resolve the requested keyboard key") from exc
        if value == 0:
            raise InvalidRequest("Unknown keyboard key", details={"key": key})
        return value

    def key(self, key: str | int, *, event: str = "tap") -> dict[str, Any]:
        if event not in {"tap", "press", "release"}:
            raise InvalidRequest("event must be tap, press, or release")
        with self._lock:
            handle = self._require_session(DEVICE_KEYBOARD)
            keyval = self._keyval(key)
            if event in {"tap", "press"}:
                self._notify(
                    "NotifyKeyboardKeysym",
                    "(oa{sv}iu)",
                    (handle, {}, keyval, KEY_PRESSED),
                )
            if event in {"tap", "release"}:
                self._notify(
                    "NotifyKeyboardKeysym",
                    "(oa{sv}iu)",
                    (handle, {}, keyval, KEY_RELEASED),
                )
            return {"performed": True, "key": key, "keysym": keyval, "event": event}

    def type_text(self, text: str, *, interval_ms: int = 10) -> dict[str, Any]:
        if not isinstance(text, str):
            raise InvalidRequest("text must be a string")
        if len(text) > 10_000:
            raise InvalidRequest("text is too long (maximum 10000 characters)")
        if type(interval_ms) is not int or not 0 <= interval_ms <= 1000:
            raise InvalidRequest("intervalMs must be an integer between 0 and 1000")
        with self._lock:
            self._require_session(DEVICE_KEYBOARD)
        for character in text:
            if self._input_cancel.is_set():
                raise BridgeError(
                    "operation_cancelled",
                    "Text input was interrupted by a stop request.",
                    409,
                )
            self.key(character)
            if interval_ms and self._input_cancel.wait(interval_ms / 1000):
                raise BridgeError(
                    "operation_cancelled",
                    "Text input was interrupted by a stop request.",
                    409,
                )
        return {"performed": True, "textLength": len(text)}

    def shutdown(self) -> None:
        try:
            self.stop_remote_desktop()
        finally:
            if self._loop is not None:
                self._loop.quit()
