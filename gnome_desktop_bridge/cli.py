"""Human- and automation-friendly client for the local bridge daemon."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from . import __version__
from .config import (
    AppPaths,
    SettingsStore,
    _atomic_write,
    load_or_create_token,
)
from .errors import BridgeError

ALL_CONFIRMATION = "ENABLE ALL DESKTOP"


class ClientError(RuntimeError):
    pass


class BridgeClient:
    """Minimal HTTP client.

    client names the caller for the daemon's audit log and agent presence;
    "control-center" and "cli-admin" are treated as the human operator.
    """

    def __init__(self, client: str = "cli", *, retries: int = 20) -> None:
        self.paths = AppPaths.discover()
        settings = SettingsStore(self.paths).get()
        url_host = f"[{settings.host}]" if ":" in settings.host else settings.host
        self.base_url = f"http://{url_host}:{settings.port}"
        self.token = load_or_create_token(self.paths)
        self.client = client
        self.retries = max(1, retries)

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-Bridge-Client": self.client,
        }

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float = 130.0,
    ) -> dict[str, Any]:
        data = None
        headers = self._headers("application/json")
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        payload: dict[str, Any] | None = None
        last_url_error: urllib.error.URLError | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                try:
                    error_payload = json.load(exc)
                    message = error_payload.get("error", {}).get("message", str(exc))
                except Exception:
                    message = str(exc)
                raise ClientError(message) from exc
            except urllib.error.URLError as exc:
                last_url_error = exc
                if (
                    not isinstance(exc.reason, ConnectionRefusedError)
                    or attempt == self.retries - 1
                ):
                    break
                # systemd considers a simple service active just before Python
                # finishes binding the loopback socket after a restart.
                time.sleep(0.1)
        if payload is None:
            reason = last_url_error.reason if last_url_error is not None else "empty response"
            raise ClientError(f"Cannot reach {self.base_url}: {reason}")
        if not payload.get("ok"):
            raise ClientError(payload.get("error", {}).get("message", "Bridge command failed"))
        return payload.get("result", {})

    def command(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(
            "/api/command",
            method="POST",
            body={"action": action, "args": args or {}},
        )

    def status(self) -> dict[str, Any]:
        return self._request("/api/status")

    def events(self, *, after: int = 0, limit: int = 200) -> dict[str, Any]:
        query = urllib.parse.urlencode({"after": after, "limit": limit})
        return self._request(f"/api/events?{query}")

    def stream_events(self, *, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield events from the SSE stream until the connection closes."""

        query = urllib.parse.urlencode({"after": after})
        request = urllib.request.Request(
            self.base_url + f"/api/events/stream?{query}",
            headers=self._headers("text/event-stream"),
        )
        try:
            with urllib.request.urlopen(request, timeout=None) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    if line.startswith("data: "):
                        yield json.loads(line[6:])
        except urllib.error.URLError as exc:
            raise ClientError(f"Event stream disconnected: {exc.reason}") from exc

    def follow_events(self, *, after: int = 0, pretty: bool = False) -> None:
        try:
            for event in self.stream_events(after=after):
                if pretty:
                    print(format_event(event), flush=True)
                else:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            return


def format_event(event: dict[str, Any]) -> str:
    """Render one audit event as a single human-readable line."""

    data = event.get("data") or {}
    stamp = str(event.get("timestamp", ""))[11:19]
    kind = str(event.get("type", ""))
    detail = ""
    if kind.startswith("command."):
        detail = str(data.get("action", ""))
        client = data.get("client")
        if client:
            detail += f"  by {client}"
        if "durationMs" in data:
            detail += f"  {data['durationMs']} ms"
        error = data.get("error")
        if isinstance(error, dict):
            detail += f"  {error.get('code')}: {error.get('message')}"
    elif kind.startswith("agent."):
        detail = f"{data.get('name', '')} ({data.get('client', '')})"
        if data.get("reason"):
            detail += f"  reason={data['reason']}"
    elif kind == "settings.changed":
        detail = f"mode={data.get('accessMode')}"
    elif data:
        detail = json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:160]
    level = str(event.get("level", "info"))
    marker = {"warning": "!", "error": "✗"}.get(level, " ")
    return f"{stamp} {marker} #{event.get('id', '?'):<6} {kind:<26} {detail}".rstrip()


def _print(value: Any, *, compact: bool = False) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        )
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return value


def _set_access(args: argparse.Namespace) -> dict[str, Any]:
    paths = AppPaths.discover()
    store = SettingsStore(paths)
    changes: dict[str, Any] = {"access_mode": args.mode}
    if args.app is not None:
        changes["allowed_apps"] = args.app
    if args.mode == "all":
        if not args.yes_i_understand:
            if not sys.stdin.isatty():
                raise ClientError(
                    "ALL DESKTOP requires --yes-i-understand or an interactive terminal"
                )
            print(
                "ALL DESKTOP permits global pointer and keyboard input after GNOME's "
                "permission dialog."
            )
            entered = input(f"Type {ALL_CONFIRMATION!r} to continue: ")
            if entered != ALL_CONFIRMATION:
                raise ClientError("Confirmation did not match; no settings were changed")
        changes["allow_portal_input"] = bool(args.portal_input)
    else:
        changes["allow_portal_input"] = False
    if args.mode == "off":
        return _stop_everything(store)
    updated = store.update(**changes)
    return updated.to_json_dict()


def _stop_everything(store: SettingsStore | None = None) -> dict[str, Any]:
    """Revoke access on disk first, then ask a running daemon to stop.

    The local write works even when the daemon is down or hung; the daemon's
    watcher also closes the portal session once it sees the new settings.
    """

    store = store or SettingsStore()
    updated, persistence_error = store.force_off()
    daemon_result: dict[str, Any] | None = None
    try:
        daemon_result = BridgeClient("cli-admin", retries=3).command("stop_all")
    except ClientError as exc:
        if persistence_error is not None:
            raise ClientError(
                f"Could not persist Off or reach the daemon: {persistence_error}; {exc}"
            ) from exc
    result = updated.to_json_dict()
    result["settingsPersisted"] = persistence_error is None
    result["daemonStopped"] = bool(daemon_result and daemon_result.get("stopped"))
    return result


def _set_feature(args: argparse.Namespace) -> dict[str, Any]:
    mapping = {
        "screenshots": "allow_screenshots",
        "portal-input": "allow_portal_input",
        "launch-apps": "allow_launch_apps",
        "persist-portal": "persist_portal_session",
        "redact-protected-text": "redact_protected_text",
        "overlay": "overlay_enabled",
        "overlay-keys": "overlay_show_keys",
        "overlay-glow": "overlay_edge_glow",
    }
    enabled = args.value == "on"
    updated = SettingsStore().update(**{mapping[args.feature]: enabled})
    return updated.to_json_dict()


def _rotate_token(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        if not sys.stdin.isatty():
            raise ClientError("Token rotation requires --yes or an interactive terminal")
        answer = input("Rotate the token and invalidate current clients? [y/N] ")
        if answer.casefold() not in {"y", "yes"}:
            raise ClientError("Token rotation cancelled")
    paths = AppPaths.discover()
    paths.ensure()
    token = secrets.token_hex(32)
    _atomic_write(paths.token_file, token + "\n")
    return {
        "rotated": True,
        "tokenFile": str(paths.token_file),
        # The daemon notices the new file on the next request.
        "restartRequired": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control GNOME Desktop Bridge")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    parser.add_argument(
        "--client",
        default="cli",
        metavar="NAME",
        help="name shown for this agent in the audit log and on-screen overlay",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("status", help="show current access and portal state")
    subparsers.add_parser("capabilities", help="probe available backends and commands")

    call = subparsers.add_parser("call", help="send any documented API command")
    call.add_argument("action")
    call.add_argument("--args", type=_parse_json_object, default={})

    events = subparsers.add_parser("events", help="read the audit event log")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=200)
    events.add_argument("--follow", action="store_true")
    events.add_argument("--pretty", action="store_true", help="one human-readable line per event")

    access = subparsers.add_parser("access", help="change the human-controlled access mode")
    access.add_argument("mode", choices=["off", "observe", "control", "all"])
    access.add_argument("--app", action="append", help="Control-mode app glob; repeat as needed")
    access.add_argument(
        "--portal-input",
        action="store_true",
        help="also enable global portal input when selecting ALL DESKTOP",
    )
    access.add_argument(
        "--yes-i-understand",
        action="store_true",
        help="non-interactively acknowledge the danger of ALL DESKTOP",
    )

    feature = subparsers.add_parser("feature", help="enable or disable a feature gate")
    feature.add_argument(
        "feature",
        choices=[
            "screenshots",
            "portal-input",
            "launch-apps",
            "persist-portal",
            "redact-protected-text",
            "overlay",
            "overlay-keys",
            "overlay-glow",
        ],
    )
    feature.add_argument("value", choices=["on", "off"])

    motion = subparsers.add_parser(
        "motion", help="set the duration of animated pointer movement (0 = instant)"
    )
    motion.add_argument("milliseconds", type=int)

    settings = subparsers.add_parser("settings", help="show settings and local paths")
    settings.add_argument("--show-token", action="store_true")

    token = subparsers.add_parser("token", help="print or rotate the persistent local token")
    token.add_argument("--rotate", action="store_true")
    token.add_argument("--yes", action="store_true")

    subparsers.add_parser(
        "stop-all",
        help="immediately stop control and switch to Off, even if the daemon is down",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "status":
            result = BridgeClient(args.client).status()
        elif args.subcommand == "capabilities":
            result = BridgeClient(args.client).command("capabilities")
        elif args.subcommand == "call":
            result = BridgeClient(args.client).command(args.action, args.args)
        elif args.subcommand == "events":
            client = BridgeClient(args.client)
            if args.follow:
                client.follow_events(after=args.after, pretty=args.pretty)
                return 0
            result = client.events(after=args.after, limit=args.limit)
            if args.pretty:
                for event in result.get("events", []):
                    print(format_event(event))
                return 0
        elif args.subcommand == "access":
            result = _set_access(args)
        elif args.subcommand == "feature":
            result = _set_feature(args)
        elif args.subcommand == "motion":
            result = SettingsStore().update(pointer_motion_ms=args.milliseconds).to_json_dict()
        elif args.subcommand == "settings":
            paths = AppPaths.discover()
            settings = SettingsStore(paths).get()
            result = {
                "settings": settings.to_json_dict(),
                "paths": {
                    "settings": str(paths.settings_file),
                    "token": str(paths.token_file),
                    "screenshots": str(paths.screenshots_dir),
                },
            }
            if args.show_token:
                result["token"] = load_or_create_token(paths)
        elif args.subcommand == "token":
            if args.rotate:
                result = _rotate_token(args)
            else:
                paths = AppPaths.discover()
                result = {"token": load_or_create_token(paths), "tokenFile": str(paths.token_file)}
        elif args.subcommand == "stop-all":
            result = _stop_everything()
        else:  # pragma: no cover - argparse enforces the choices
            parser.error("unknown subcommand")
            return 2
        _print(result, compact=args.compact)
        return 0
    except (ClientError, BridgeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
