"""Loopback-only HTTP/JSON daemon for GNOME Desktop Bridge."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hmac
import json
import logging
import os
import signal
import socket
import stat
import sys
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .commands import CommandDispatcher
from .config import AppPaths, SettingsStore, load_or_create_token
from .errors import BridgeError, InvalidRequest
from .events import EventBuffer

MAX_REQUEST_BODY = 1024 * 1024
LOGGER = logging.getLogger("gnome-desktop-bridge")


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        token: str,
        dispatcher: CommandDispatcher,
        events: EventBuffer,
    ) -> None:
        self.token = token
        self.dispatcher = dispatcher
        self.events = events
        super().__init__(server_address, handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(30.0)
        return request, client_address


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GNOMEDesktopBridge"
    sys_version = ""

    @property
    def bridge_server(self) -> BridgeHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "service": "gnome-desktop-bridge", "version": __version__},
            )
            return
        if not self._valid_host():
            self.close_connection = True
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_host", "Invalid Host header")
            return
        if not self._authenticate():
            return
        if parsed.path == "/api/status":
            self._send_json(
                HTTPStatus.OK, {"ok": True, "result": self.bridge_server.dispatcher.state()}
            )
            return
        if parsed.path == "/api/events":
            try:
                query = parse_qs(parsed.query)
                after = self._query_int(query, "after", 0, minimum=0)
                limit = self._query_int(query, "limit", 200, minimum=1, maximum=1000)
                result = {
                    "events": self.bridge_server.events.after(after, limit=limit),
                    "latestEventId": self.bridge_server.events.latest_id(),
                }
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
            except BridgeError as exc:
                self._send_bridge_error(exc)
            return
        if parsed.path == "/api/events/stream":
            try:
                query = parse_qs(parsed.query)
                header_after = self.headers.get("Last-Event-ID", "0")
                default_after = int(header_after) if header_after.isdigit() else 0
                after = self._query_int(query, "after", default_after, minimum=0)
                self._serve_event_stream(after)
            except BridgeError as exc:
                self._send_bridge_error(exc)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if not self._valid_host():
            self.close_connection = True
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_host", "Invalid Host header")
            return
        if not self._authenticate():
            return
        if parsed.path != "/api/command":
            self.close_connection = True
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
            return
        try:
            payload = self._read_json_body()
            result = self.bridge_server.dispatcher.dispatch(payload)
            self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
        except BridgeError as exc:
            self._send_bridge_error(exc)
        except Exception:
            error_id = uuid.uuid4().hex[:12]
            LOGGER.exception("Unhandled command error (%s)", error_id)
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "An internal daemon error occurred",
                details={"errorId": error_id},
            )

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # Intentionally do not enable CORS. A random website must not gain access
        # to a desktop-control API even when it can reach localhost.
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "cors_disabled",
            "Browser cross-origin access is not enabled",
        )

    def _valid_host(self) -> bool:
        raw = self.headers.get("Host", "")
        if not raw:
            return False
        if raw.startswith("[") and "]" in raw:
            host = raw[1 : raw.index("]")]
        else:
            host = raw.rsplit(":", 1)[0] if raw.count(":") <= 1 else raw
        return host.casefold() in {"127.0.0.1", "localhost", "::1"}

    def _authenticate(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, separator, candidate = header.partition(" ")
        valid = (
            bool(separator)
            and scheme.casefold() == "bearer"
            and hmac.compare_digest(candidate.strip(), self.bridge_server.token)
        )
        if not valid:
            # Do not try to reuse a connection whose unauthenticated request body
            # has intentionally not been read.
            self.close_connection = True
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Bearer realm="gnome-desktop-bridge"')
            body = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "unauthorized",
                        "message": "A valid local bearer token is required",
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            raise InvalidRequest("Transfer-Encoding is not supported")
        raw_lengths = self.headers.get_all("Content-Length", [])
        if len(raw_lengths) != 1:
            self.close_connection = True
            raise InvalidRequest("Content-Length is required")
        raw_length = raw_lengths[0]
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise InvalidRequest("Content-Length must be an integer") from exc
        if length < 0 or length > MAX_REQUEST_BODY:
            self.close_connection = True
            raise BridgeError(
                "request_too_large",
                f"Request body exceeds {MAX_REQUEST_BODY} bytes",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self.close_connection = True
            raise BridgeError(
                "unsupported_media_type",
                "Content-Type must be application/json",
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidRequest("Request body is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise InvalidRequest("Command body must be a JSON object")
        return payload

    @staticmethod
    def _query_int(
        query: dict[str, list[str]],
        name: str,
        default: int,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        raw = query.get(name, [str(default)])[0]
        try:
            value = int(raw)
        except ValueError as exc:
            raise InvalidRequest(f"Query parameter {name} must be an integer") from exc
        if value < minimum or (maximum is not None and value > maximum):
            suffix = f" and {maximum}" if maximum is not None else ""
            raise InvalidRequest(f"Query parameter {name} must be between {minimum}{suffix}")
        return value

    def _serve_event_stream(self, after: int) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        event_id = after
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                events = self.bridge_server.events.wait_after(event_id, timeout=15.0, limit=200)
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    packet = (
                        f"id: {event['id']}\nevent: {event['type']}\ndata: {data}\n\n"
                    ).encode("utf-8")
                    self.wfile.write(packet)
                    event_id = int(event["id"])
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _send_bridge_error(self, error: BridgeError) -> None:
        self._send_json(error.status, {"ok": False, "error": error.as_dict()})

    def _send_error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if details:
            error["details"] = details
        self._send_json(status, {"ok": False, "error": error})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), format_string % args)


class InstanceLock:
    def __init__(self, paths: AppPaths) -> None:
        paths.ensure()
        self.path = paths.runtime_dir / "daemon.lock"
        self.handle: Any | None = None

    def acquire(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(fd)
            raise RuntimeError("The daemon lock must be a user-owned regular file")
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "r+", encoding="ascii")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError(
                    "Another GNOME Desktop Bridge daemon is already running"
                ) from exc
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def _server_class_for(host: str) -> type[BridgeHTTPServer]:
    if ":" not in host:
        return BridgeHTTPServer

    class IPv6BridgeHTTPServer(BridgeHTTPServer):
        address_family = socket.AF_INET6

    return IPv6BridgeHTTPServer


def run_daemon() -> int:
    paths = AppPaths.discover()
    paths.ensure()
    lock = InstanceLock(paths)
    lock.acquire()
    settings_store = SettingsStore(paths)
    initial_settings = settings_store.get()
    startup_downgraded = initial_settings.access_mode == "all"
    if startup_downgraded:
        # ALL DESKTOP is deliberately session-scoped. A crash, logout, reboot,
        # or manual service restart requires fresh human elevation.
        initial_settings = settings_store.update(
            access_mode="off",
            allow_portal_input=False,
        )
    token = load_or_create_token(paths)
    events = EventBuffer(initial_settings.max_events)
    dispatcher = CommandDispatcher(settings_store, events)
    server_class = _server_class_for(initial_settings.host)
    server = server_class(
        (initial_settings.host, initial_settings.port),
        BridgeHandler,
        token=token,
        dispatcher=dispatcher,
        events=events,
    )
    stop_watcher = threading.Event()

    def policy_watcher() -> None:
        while not stop_watcher.wait(1.0):
            try:
                dispatcher.enforce_current_policy()
            except Exception:
                LOGGER.exception("Policy watcher failed")

    watcher = threading.Thread(target=policy_watcher, name="policy-watcher", daemon=True)
    watcher.start()

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, name="signal-shutdown", daemon=True).start()

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_shutdown)

    events.emit(
        "daemon.started",
        data={
            "version": __version__,
            "pid": os.getpid(),
            "host": initial_settings.host,
            "port": initial_settings.port,
            "accessMode": initial_settings.access_mode,
        },
    )
    if startup_downgraded:
        events.emit(
            "security.startup_downgrade",
            level="warning",
            data={"previousMode": "all", "accessMode": "off"},
        )
    LOGGER.info(
        "GNOME Desktop Bridge %s listening on http://%s:%s (mode: %s)",
        __version__,
        initial_settings.host,
        initial_settings.port,
        initial_settings.access_mode,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        events.emit("daemon.stopping")
        stop_watcher.set()
        server.server_close()
        dispatcher.shutdown()
        lock.release()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GNOME Desktop Bridge local daemon")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return run_daemon()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOGGER.error("Cannot start daemon: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
