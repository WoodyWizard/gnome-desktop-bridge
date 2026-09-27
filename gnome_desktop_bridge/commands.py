"""Command dispatcher joining policy, semantic access, portals, and audit logs."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .app_backend import AppBackend
from .atspi_backend import AtspiBackend
from .config import Settings, SettingsStore
from .errors import BridgeError, InvalidRequest
from .events import EventBuffer
from .policy import (
    ALWAYS_ACTIONS,
    GLOBAL_CONTROL_ACTIONS,
    OBSERVE_ACTIONS,
    SCOPED_CONTROL_ACTIONS,
    AccessLevel,
    LEVEL_BY_NAME,
    Policy,
)
from .portal_backend import PortalBackend
from .shell_backend import ShellBackend, wayland_session

ACTION_ARGUMENTS: dict[str, set[str]] = {
    "ping": set(),
    "capabilities": set(),
    "get_state": set(),
    "stop_all": set(),
    "hello": {"name"},
    "goodbye": set(),
    "list_apps": set(),
    "snapshot": {"ref", "depth", "maxNodes", "includeText"},
    "screenshot": {"interactive", "includeBase64"},
    "list_launchers": {"query", "limit"},
    "launch_app": {"desktopId"},
    "invoke": {"ref", "action"},
    "fill": {"ref", "text"},
    "focus": {"ref"},
    "click": {"ref", "method", "action", "button", "count"},
    "start_remote_desktop": set(),
    "stop_remote_desktop": set(),
    "pointer_move": {"x", "y", "durationMs"},
    "pointer_click": {"button", "x", "y", "count", "durationMs"},
    "scroll": {"dx", "dy", "discrete", "x", "y"},
    "key": {"key", "event", "modifiers"},
    "type_text": {"text", "intervalMs"},
}

# Clients that act on behalf of the human, not an automation agent. Their
# commands never make the bridge announce that an agent is connected.
INTERNAL_CLIENTS = frozenset({"control-center", "overlay", "cli-admin"})
ANONYMOUS_CLIENTS = frozenset({"api", "cli"})
AGENT_IDLE_TIMEOUT = 120.0
SCREENSHOT_OVERLAY_HIDE_S = 0.15

MODIFIER_KEYS = frozenset(
    {
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Super_L",
        "Super_R",
        "Meta_L",
        "Meta_R",
        "Shift_L",
        "Shift_R",
        "ISO_Level3_Shift",
    }
)
SHORTCUT_MODIFIERS = MODIFIER_KEYS - {"Shift_L", "Shift_R", "ISO_Level3_Shift"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidRequest(f"{name} must be a JSON object")
    return value


def _int_arg(
    args: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = args.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidRequest(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _bool_arg(args: dict[str, Any], name: str, default: bool) -> bool:
    value = args.get(name, default)
    if type(value) is not bool:
        raise InvalidRequest(f"{name} must be a boolean")
    return value


def _number_arg(args: dict[str, Any], name: str, default: float | None = None) -> float:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequest(f"{name} must be a number")
    if not math.isfinite(value):
        raise InvalidRequest(f"{name} must be finite")
    return float(value)


def _optional_point(args: dict[str, Any]) -> tuple[float, float] | None:
    if "x" not in args and "y" not in args:
        return None
    if "x" not in args or "y" not in args:
        raise InvalidRequest("x and y must be provided together")
    return _number_arg(args, "x"), _number_arg(args, "y")


def _action_selector(args: dict[str, Any]) -> int | str:
    requested = args.get("action", 0)
    if not isinstance(requested, (str, int)) or type(requested) is bool:
        raise InvalidRequest("args.action must be an action name or integer index")
    if isinstance(requested, str) and len(requested) > 500:
        raise InvalidRequest("args.action is too long")
    return requested


def _agent_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest("name must be a non-empty string")
    name = " ".join(value.split())
    if len(name) > 64 or not name.isprintable():
        raise InvalidRequest("name must be printable and at most 64 characters")
    return name


def _sanitize_for_audit(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Bound and redact client-controlled data before it enters the event buffer."""

    folded_key = key.casefold()
    sensitive_markers = {
        "authorization",
        "base64",
        "clipboard",
        "password",
        "secret",
        "text",
        "token",
    }
    if any(marker in folded_key for marker in sensitive_markers):
        length = len(value) if isinstance(value, (str, bytes, list, dict)) else None
        return {"redacted": True, "length": length}
    if depth >= 6:
        return "[TRUNCATED:DEPTH]"
    if isinstance(value, dict):
        selected = list(value.items())[:50]
        result = {
            str(item_key)[:100]: _sanitize_for_audit(
                item_value,
                key=str(item_key),
                depth=depth + 1,
            )
            for item_key, item_value in selected
        }
        if len(value) > 50:
            result["_truncatedEntries"] = len(value) - 50
        return result
    if isinstance(value, (list, tuple)):
        result = [_sanitize_for_audit(item, depth=depth + 1) for item in value[:50]]
        if len(value) > 50:
            result.append(f"[TRUNCATED:{len(value) - 50}]")
        return result
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + "…[TRUNCATED]"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return f"<{type(value).__name__}>"


@dataclass(slots=True)
class AgentPresence:
    client: str
    name: str
    since: str
    last_seen: float
    commands: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "name": self.name,
            "since": self.since,
            "idleSeconds": round(max(0.0, time.monotonic() - self.last_seen), 1),
            "commands": self.commands,
        }


class CommandDispatcher:
    def __init__(
        self,
        settings: SettingsStore,
        events: EventBuffer,
        *,
        atspi: AtspiBackend | None = None,
        portal: PortalBackend | None = None,
        apps: AppBackend | None = None,
        shell: ShellBackend | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.atspi = atspi or AtspiBackend()
        self.portal = portal or PortalBackend(settings.paths)
        self.apps = apps or AppBackend()
        self.shell = shell or ShellBackend()
        self.policy = policy or Policy()
        self._presence_lock = threading.Lock()
        self._presence: AgentPresence | None = None
        self._watch_lock = threading.Lock()
        self._last_settings: dict[str, Any] | None = None
        self._portal_reported_active = False
        self._revocation_announced = False
        self._held_modifiers: set[str] = set()

    # ----------------------------------------------------------------- dispatch

    def dispatch(self, payload: dict[str, Any], *, client: str = "api") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise InvalidRequest("Command body must be a JSON object")
        unexpected_payload = sorted(set(payload) - {"action", "args"})
        if unexpected_payload:
            raise InvalidRequest(
                "Unexpected top-level command fields",
                details={"fields": unexpected_payload},
            )
        action = payload.get("action")
        if not isinstance(action, str) or not action:
            raise InvalidRequest("action must be a non-empty string")
        if len(action) > 100:
            raise InvalidRequest("action is too long")
        args = _require_dict(payload.get("args", {}), "args")
        allowed_arguments = ACTION_ARGUMENTS.get(action)
        if allowed_arguments is None:
            raise InvalidRequest(f"Unknown action: {action}")
        unexpected = sorted(set(args) - allowed_arguments)
        if unexpected:
            raise InvalidRequest(
                f"Unexpected arguments for {action}",
                details={"arguments": unexpected},
            )
        if action == "click":
            method = args.get("method", "action")
            if not isinstance(method, str) or method not in {"action", "coordinates"}:
                raise InvalidRequest("method must be action or coordinates")
            if method == "action" and ("button" in args or "count" in args):
                raise InvalidRequest("button and count are valid only for a coordinates click")
            if method == "coordinates" and "action" in args:
                raise InvalidRequest("args.action is valid only for an AT-SPI action click")
        hello_name = _agent_name(args.get("name")) if action == "hello" else None
        settings = self.settings.get()
        self.events.resize(settings.max_events)

        if action not in {"goodbye", "stop_all"}:
            self._touch_agent(client, hello_name)
        request_event = self.events.emit(
            "command.started",
            data={"action": action, "client": client, "args": self._audit_args(action, args)},
        )
        started = time.monotonic()
        try:
            result = self._execute(action, args, settings, client=client)
        except BridgeError as exc:
            self.events.emit(
                "command.failed",
                level="warning" if exc.status < 500 else "error",
                data={
                    "action": action,
                    "client": client,
                    "requestEventId": request_event["id"],
                    "durationMs": round((time.monotonic() - started) * 1000, 2),
                    "error": _sanitize_for_audit(exc.as_dict()),
                },
            )
            raise
        except Exception as exc:
            self.events.emit(
                "command.failed",
                level="error",
                data={
                    "action": action,
                    "client": client,
                    "requestEventId": request_event["id"],
                    "durationMs": round((time.monotonic() - started) * 1000, 2),
                    "error": {"code": "internal_error", "message": type(exc).__name__},
                },
            )
            raise
        self.events.emit(
            "command.completed",
            data={
                "action": action,
                "client": client,
                "requestEventId": request_event["id"],
                "durationMs": round((time.monotonic() - started) * 1000, 2),
                "result": self._audit_result(action, result),
            },
        )
        return result

    def _execute(
        self,
        action: str,
        args: dict[str, Any],
        settings: Settings,
        *,
        client: str,
    ) -> dict[str, Any]:
        if action == "ping":
            self.policy.authorize(action, settings)
            return {"pong": True, "version": __version__, "timestamp": _utc_now()}
        if action == "capabilities":
            self.policy.authorize(action, settings)
            return self.capabilities()
        if action == "get_state":
            self.policy.authorize(action, settings)
            return self.state(settings)
        if action == "stop_all":
            self.policy.authorize(action, settings)
            return self.stop_all(source="api" if client not in INTERNAL_CLIENTS else client)
        if action == "hello":
            self.policy.authorize(action, settings)
            with self._presence_lock:
                agent = self._presence.as_dict() if self._presence else None
            return {"agent": agent, "accessMode": settings.access_mode}
        if action == "goodbye":
            self.policy.authorize(action, settings)
            ended = self._end_agent("goodbye") if client not in INTERNAL_CLIENTS else False
            return {"disconnected": ended}

        if action == "list_apps":
            self.policy.authorize(action, settings)
            return self.atspi.list_apps()
        if action == "snapshot":
            self.policy.authorize(action, settings)
            ref = self._ref(args)
            depth = _int_arg(args, "depth", 8, minimum=0, maximum=30)
            max_nodes = _int_arg(args, "maxNodes", 1000, minimum=1, maximum=5000)
            include_text = _bool_arg(args, "includeText", True)
            self._visual_target(settings, "scan", ref)
            return self.atspi.snapshot(
                ref,
                depth=depth,
                max_nodes=max_nodes,
                include_text=include_text,
                redact_protected_text=settings.redact_protected_text,
            )
        if action == "screenshot":
            self.policy.authorize(action, settings)
            interactive = _bool_arg(args, "interactive", False)
            include_base64 = _bool_arg(args, "includeBase64", False)
            if self._visual(settings, "screenshot", {"phase": "before"}):
                # Give the overlay a moment to hide so it is not captured.
                time.sleep(SCREENSHOT_OVERLAY_HIDE_S)
            try:
                return self.portal.screenshot(
                    interactive=interactive,
                    include_base64=include_base64,
                )
            finally:
                self._visual(settings, "screenshot", {"phase": "after"})
        if action == "list_launchers":
            self.policy.authorize(action, settings)
            query = args.get("query", "")
            if not isinstance(query, str):
                raise InvalidRequest("query must be a string")
            if len(query) > 1000:
                raise InvalidRequest("query is too long")
            return self.apps.list_launchers(
                query=query,
                limit=_int_arg(args, "limit", 300, minimum=1, maximum=1000),
            )

        if action == "launch_app":
            self._require_control_mode(action, settings)
            desktop_id = args.get("desktopId")
            app, identity = self.apps.resolve(desktop_id)
            self.policy.authorize(action, settings, app=identity)
            return self.apps.launch_resolved(app, identity)

        if action in {"invoke", "fill", "focus", "click"}:
            self._require_control_mode(action, settings)
            ref = self._ref(args)
            identity = self.atspi.identity_for_ref(ref)
            self.policy.authorize(action, settings, app=identity)
            if action == "invoke":
                requested_action = _action_selector(args)
                self._visual_target(settings, "invoke", ref)
                return self.atspi.invoke(ref, action=requested_action)
            if action == "fill":
                text = args.get("text")
                self._visual_target(settings, "fill", ref)
                return self.atspi.fill(ref, text)
            if action == "focus":
                self._visual_target(settings, "focus", ref)
                return self.atspi.focus(ref)
            return self._semantic_click(ref, args, settings)

        if action == "start_remote_desktop":
            self.policy.authorize(action, settings)
            result = self.portal.start_remote_desktop(persist=settings.persist_portal_session)
            self._report_portal(True, "portal.session.started", result)
            return result
        if action == "stop_remote_desktop":
            self.policy.authorize(action, settings)
            result = self.portal.stop_remote_desktop()
            self._report_portal(False, "portal.session.stopped")
            return result
        if action == "pointer_move":
            self.policy.authorize(action, settings)
            x, y = _number_arg(args, "x"), _number_arg(args, "y")
            duration = self._motion_ms(args, settings)
            self._visual_pointer(settings, x, y, duration)
            return self.portal.pointer_move(x, y, duration_ms=duration)
        if action == "pointer_click":
            self.policy.authorize(action, settings)
            point = _optional_point(args)
            button = args.get("button", "left")
            count = _int_arg(args, "count", 1, minimum=1, maximum=3)
            duration = self._motion_ms(args, settings)
            if point is not None:
                self._visual_pointer(settings, point[0], point[1], duration)
            result = self.portal.pointer_click(
                button=button,
                x=point[0] if point else None,
                y=point[1] if point else None,
                count=count,
                duration_ms=duration,
            )
            self._visual_click(settings, result, button, count)
            return result
        if action == "scroll":
            self.policy.authorize(action, settings)
            point = _optional_point(args)
            dx, dy = _number_arg(args, "dx", 0.0), _number_arg(args, "dy", 0.0)
            discrete = _bool_arg(args, "discrete", False)
            if point is not None:
                duration = settings.pointer_motion_ms
                self._visual_pointer(settings, point[0], point[1], duration)
                self.portal.pointer_move(point[0], point[1], duration_ms=duration)
            result = self.portal.scroll(dx, dy, discrete=discrete)
            self._visual(settings, "scroll", {"dx": dx, "dy": dy, "discrete": discrete})
            return result
        if action == "key":
            self.policy.authorize(action, settings)
            key = args.get("key")
            event = args.get("event", "tap")
            modifiers = args.get("modifiers", [])
            result = self.portal.key(key, event=event, modifiers=modifiers)
            self._visual_key(settings, key, event, modifiers)
            return result
        if action == "type_text":
            self.policy.authorize(action, settings)
            text = args.get("text")
            interval = _int_arg(args, "intervalMs", 10, minimum=0, maximum=1000)
            if isinstance(text, str):
                self._visual(
                    settings,
                    "type",
                    {"length": len(text), "estimatedMs": len(text) * max(interval, 2)},
                )
            return self.portal.type_text(text, interval_ms=interval)

        # Policy owns the canonical action list and returns a useful error.
        self.policy.authorize(action, settings)
        raise InvalidRequest(f"Action is not implemented: {action}")

    def _semantic_click(
        self,
        ref: str,
        args: dict[str, Any],
        settings: Settings,
    ) -> dict[str, Any]:
        method = args.get("method", "action")
        if method == "action":
            requested_action = _action_selector(args)
            self._visual_target(settings, "click", ref)
            result = self.atspi.invoke(ref, action=requested_action)
            result["method"] = "atspi-action"
            return result

        # Coordinates are global desktop control even when derived from a scoped
        # semantic element, so require ALL DESKTOP independently.
        self.policy.authorize("pointer_click", settings)
        count = _int_arg(args, "count", 1, minimum=1, maximum=3)
        button = args.get("button", "left")
        info = self.atspi.describe_ref(ref)
        bounds, verified = self._screen_bounds(info)
        if bounds is None:
            raise InvalidRequest("The target has no usable screen coordinates")
        if not verified:
            raise BridgeError(
                "coordinates_unavailable",
                "On Wayland, AT-SPI reports window-relative coordinates. Enable the "
                "GNOME Desktop Bridge Shell extension so the bridge can locate the "
                "window, or use method=action.",
                409,
                {"ref": ref},
            )
        x = bounds["x"] + bounds["width"] / 2
        y = bounds["y"] + bounds["height"] / 2
        duration = settings.pointer_motion_ms
        self._visual_target(settings, "click", ref, info=info, bounds=bounds)
        self._visual_pointer(settings, x, y, duration)
        result = self.portal.pointer_click(
            button=button, x=x, y=y, count=count, duration_ms=duration
        )
        self._visual_click(settings, result, button, count)
        result.update({"method": "portal-coordinates", "ref": ref, "bounds": bounds})
        return result

    @staticmethod
    def _ref(args: dict[str, Any]) -> str:
        ref = args.get("ref")
        if not isinstance(ref, str) or not ref:
            raise InvalidRequest("args.ref must be a non-empty semantic reference")
        if len(ref) > 100:
            raise InvalidRequest("args.ref is too long")
        return ref

    @staticmethod
    def _motion_ms(args: dict[str, Any], settings: Settings) -> int:
        return _int_arg(args, "durationMs", settings.pointer_motion_ms, minimum=0, maximum=2000)

    def _require_control_mode(self, action: str, settings: Settings) -> None:
        # This check happens before resolving an AT-SPI reference, so Off/Observe
        # callers cannot probe whether a target reference exists.
        if LEVEL_BY_NAME[settings.access_mode] < AccessLevel.CONTROL:
            self.policy.authorize(action, settings, app=None)

    # ---------------------------------------------------------- coordinate map

    def _screen_bounds(self, info: dict[str, Any]) -> tuple[dict[str, int] | None, bool]:
        """Translate AT-SPI bounds to global logical coordinates.

        Returns the bounds and whether they are known to be global.
        """

        bounds = info.get("bounds")
        if not bounds or bounds.get("width", 0) <= 0 or bounds.get("height", 0) <= 0:
            return None, False
        window = info.get("window")
        app = info.get("app")
        if window:
            window_bounds = window["bounds"]
            origin = self.shell.window_origin(
                getattr(app, "pid", None),
                window.get("name", ""),
                window_bounds["width"],
                window_bounds["height"],
            )
            if origin is not None:
                needs_offset, origin_x, origin_y = origin
                if not needs_offset:
                    return dict(bounds), True
                return (
                    {
                        "x": bounds["x"] - window_bounds["x"] + origin_x,
                        "y": bounds["y"] - window_bounds["y"] + origin_y,
                        "width": bounds["width"],
                        "height": bounds["height"],
                    },
                    True,
                )
        return dict(bounds), not wayland_session()

    # ----------------------------------------------------------- overlay cues

    def _overlay_active(self, settings: Settings) -> bool:
        return settings.overlay_enabled and self.events.subscriber_count("overlay") > 0

    def _visual(self, settings: Settings, kind: str, data: dict[str, Any]) -> bool:
        """Emit an on-screen feedback cue; returns whether an overlay listens."""

        if not self._overlay_active(settings):
            return False
        self.events.emit(f"visual.{kind}", data=data)
        return True

    def _visual_pointer(self, settings: Settings, x: float, y: float, duration: int) -> None:
        self._visual(settings, "pointer", {"x": x, "y": y, "durationMs": duration})

    def _visual_click(
        self,
        settings: Settings,
        result: dict[str, Any],
        button: Any,
        count: int,
    ) -> None:
        self._visual(
            settings,
            "click",
            {
                "x": result.get("x"),
                "y": result.get("y"),
                "button": button if isinstance(button, (str, int)) else "left",
                "count": count,
            },
        )

    def _visual_target(
        self,
        settings: Settings,
        action: str,
        ref: str,
        *,
        info: dict[str, Any] | None = None,
        bounds: dict[str, int] | None = None,
    ) -> None:
        if not self._overlay_active(settings):
            return
        try:
            if info is None:
                info = self.atspi.describe_ref(ref)
            if bounds is None:
                bounds, verified = self._screen_bounds(info)
            else:
                verified = True
        except Exception:
            # Feedback is best effort and must never fail the command itself.
            return
        if bounds is None or not verified:
            return
        self._visual(
            settings,
            "target",
            {
                "action": action,
                "bounds": bounds,
                "role": str(info.get("role", ""))[:100],
                "name": str(info.get("name", ""))[:80],
            },
        )

    def _visual_key(
        self,
        settings: Settings,
        key: Any,
        event: str,
        modifiers: list[Any],
    ) -> None:
        name = str(key)
        if event == "press" and name in MODIFIER_KEYS:
            self._held_modifiers.add(name)
        elif event == "release":
            self._held_modifiers.discard(name)
        if not self._overlay_active(settings) or event == "release":
            return
        held = sorted(self._held_modifiers | {str(item) for item in modifiers})
        if not settings.overlay_show_keys:
            keys: list[str] = []
        else:
            # A lone printable character may be part of a secret being typed;
            # only reveal it when it forms a shortcut with a real modifier.
            printable = len(name) == 1 and name.isprintable()
            shortcut = any(item in SHORTCUT_MODIFIERS for item in held)
            label = "•" if printable and not shortcut and name not in {" "} else name
            keys = [item for item in held if item != name] + [label]
        self._visual(settings, "key", {"keys": keys, "event": event})

    # ---------------------------------------------------------------- presence

    def _touch_agent(self, client: str, hello_name: str | None) -> None:
        if client in INTERNAL_CLIENTS:
            return
        now = time.monotonic()
        with self._presence_lock:
            presence = self._presence
            if presence is not None and presence.client != client:
                self._emit_disconnected(presence, "replaced")
                presence = None
            if presence is not None and hello_name and hello_name != presence.name:
                presence.name = hello_name
                self.events.emit("agent.renamed", data={"client": client, "name": hello_name})
            if presence is None:
                if hello_name:
                    name = hello_name
                elif client in ANONYMOUS_CLIENTS:
                    name = "AI agent"
                else:
                    name = client
                presence = AgentPresence(client=client, name=name, since=_utc_now(), last_seen=now)
                self._presence = presence
                self.events.emit("agent.connected", data={"client": client, "name": name})
            presence.last_seen = now
            presence.commands += 1

    def _emit_disconnected(self, presence: AgentPresence, reason: str) -> None:
        self.events.emit(
            "agent.disconnected",
            data={
                "client": presence.client,
                "name": presence.name,
                "reason": reason,
                "commands": presence.commands,
            },
        )

    def _end_agent(self, reason: str) -> bool:
        with self._presence_lock:
            presence, self._presence = self._presence, None
            if presence is None:
                return False
            self._emit_disconnected(presence, reason)
            self._held_modifiers.clear()
            return True

    def agent(self) -> dict[str, Any] | None:
        with self._presence_lock:
            return self._presence.as_dict() if self._presence else None

    # ------------------------------------------------------------------- state

    def capabilities(self) -> dict[str, Any]:
        atspi_available, atspi_error = self.atspi.available()
        portal_available, portal_error = self.portal.available()
        return {
            "version": __version__,
            "transport": {
                "protocol": "http-json+sse",
                "authentication": "bearer-token",
                "networkScope": "loopback-only",
            },
            "modes": ["off", "observe", "control", "all"],
            "backends": {
                "atspi": {"available": atspi_available, "error": atspi_error},
                "portal": {
                    "available": portal_available,
                    "error": portal_error,
                    "versions": self.portal.versions() if portal_available else {},
                },
                "applicationLaunchers": {"available": True, "backend": "Gio.AppInfo"},
                "shellExtension": {
                    "overlayConnected": self.events.subscriber_count("overlay") > 0,
                },
            },
            "actions": {
                "always": sorted(ALWAYS_ACTIONS),
                "observe": sorted(OBSERVE_ACTIONS),
                "scopedControl": sorted(SCOPED_CONTROL_ACTIONS),
                "allDesktop": sorted(GLOBAL_CONTROL_ACTIONS),
            },
        }

    @staticmethod
    def _public_settings(settings: Settings) -> dict[str, Any]:
        return {
            "accessMode": settings.access_mode,
            "allowedApps": list(settings.allowed_apps or []),
            "features": {
                "screenshots": settings.allow_screenshots,
                "portalInput": settings.allow_portal_input,
                "launchApps": settings.allow_launch_apps,
                "persistPortalSession": settings.persist_portal_session,
                "redactProtectedText": settings.redact_protected_text,
            },
            "overlay": {
                "enabled": settings.overlay_enabled,
                "showKeys": settings.overlay_show_keys,
                "edgeGlow": settings.overlay_edge_glow,
                "pointerMotionMs": settings.pointer_motion_ms,
            },
        }

    def state(self, settings: Settings | None = None) -> dict[str, Any]:
        settings = settings or self.settings.get()
        result = {"version": __version__, **self._public_settings(settings)}
        result["overlay"]["clients"] = self.events.subscriber_count("overlay")
        result.update(
            {
                "remoteDesktop": self.portal.status(),
                "agent": self.agent(),
                "latestEventId": self.events.latest_id(),
                "settingsError": self.settings.last_error,
            }
        )
        return result

    def stop_all(self, *, source: str) -> dict[str, Any]:
        portal_error: str | None = None
        try:
            self.portal.stop_remote_desktop()
        except Exception as exc:  # stopping must remain best-effort and fail closed
            portal_error = type(exc).__name__
        updated, settings_error = self.settings.force_off()
        self._report_portal(False)
        self.events.emit(
            "security.stop_all",
            level="warning",
            data={
                "source": source,
                "portalError": portal_error,
                "settingsPersistenceError": settings_error,
            },
        )
        self._end_agent("stop_all")
        return {
            "stopped": True,
            "accessMode": updated.access_mode,
            "portalError": portal_error,
            "settingsPersisted": settings_error is None,
        }

    def _report_portal(
        self,
        active: bool,
        event_type: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._watch_lock:
            self._portal_reported_active = active
        if event_type is not None:
            self.events.emit(event_type, data=data)

    def enforce_current_policy(self) -> None:
        """Close a still-active portal session as soon as ALL mode is revoked."""

        settings = self.settings.get()
        control_in_progress = getattr(self.portal, "control_in_progress", self.portal.active)
        if not control_in_progress:
            self._revocation_announced = False
            return
        if settings.access_mode != "all" or not settings.allow_portal_input:
            self.portal.stop_remote_desktop()
            self._report_portal(False)
            # A cancelled start can take a moment to wind down; announce once.
            if not self._revocation_announced:
                self._revocation_announced = True
                self.events.emit(
                    "portal.session.revoked",
                    level="warning",
                    data={"reason": "settings_changed", "accessMode": settings.access_mode},
                )

    def tick(self) -> None:
        """Periodic housekeeping, run by the daemon's watcher thread."""

        self.enforce_current_policy()
        with self._presence_lock:
            presence = self._presence
            expired = (
                presence is not None
                and time.monotonic() - presence.last_seen > AGENT_IDLE_TIMEOUT
            )
        if expired:
            self._end_agent("idle")
        settings = self.settings.get()
        snapshot = settings.to_json_dict()
        active = bool(self.portal.active)
        with self._watch_lock:
            settings_changed = (
                self._last_settings is not None and snapshot != self._last_settings
            )
            self._last_settings = snapshot
            # GNOME can end the session on its own, e.g. from the top-bar
            # screen-sharing indicator.
            portal_ended = self._portal_reported_active and not active
            if portal_ended:
                self._portal_reported_active = False
        if settings_changed:
            self.events.emit("settings.changed", data=self._public_settings(settings))
        if portal_ended:
            self.events.emit("portal.session.ended", data={"reason": "closed_by_desktop"})

    # ------------------------------------------------------------------- audit

    @staticmethod
    def _audit_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
        result = _sanitize_for_audit(args)
        if action in {"fill", "type_text"} and "text" in result:
            text = args.get("text")
            result.pop("text", None)
            result["textRedacted"] = True
            result["textLength"] = len(text) if isinstance(text, str) else None
        return result

    @staticmethod
    def _audit_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
        if action == "screenshot":
            return {key: value for key, value in result.items() if key != "base64"}
        if action == "snapshot":
            return {
                "generation": result.get("generation"),
                "nodeCount": result.get("nodeCount"),
                "truncated": result.get("truncated"),
            }
        if action in {"list_apps", "list_launchers"}:
            key = "apps" if action == "list_apps" else "launchers"
            return {"count": len(result.get(key, []))}
        if action in {"get_state", "capabilities"}:
            return {"returned": True}
        return _sanitize_for_audit(result)

    def shutdown(self) -> None:
        self.portal.shutdown()
