"""Command dispatcher joining policy, semantic access, portals, and audit logs."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .app_backend import AppBackend
from .atspi_backend import AtspiBackend
from .config import Settings, SettingsStore
from .errors import BridgeError, InvalidRequest
from .events import EventBuffer
from .policy import (
    GLOBAL_CONTROL_ACTIONS,
    SCOPED_CONTROL_ACTIONS,
    AccessLevel,
    LEVEL_BY_NAME,
    Policy,
)
from .portal_backend import PortalBackend

ACTION_ARGUMENTS: dict[str, set[str]] = {
    "ping": set(),
    "capabilities": set(),
    "get_state": set(),
    "stop_all": set(),
    "list_apps": set(),
    "snapshot": {"ref", "depth", "maxNodes", "includeText"},
    "screenshot": {"interactive", "includeBase64"},
    "list_launchers": {"query", "limit"},
    "launch_app": {"desktopId"},
    "invoke": {"ref", "action"},
    "fill": {"ref", "text"},
    "focus": {"ref"},
    "click": {"ref", "method", "action", "button"},
    "start_remote_desktop": set(),
    "stop_remote_desktop": set(),
    "pointer_move": {"x", "y"},
    "pointer_click": {"button", "x", "y"},
    "scroll": {"dx", "dy"},
    "key": {"key", "event"},
    "type_text": {"text", "intervalMs"},
}


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


class CommandDispatcher:
    def __init__(
        self,
        settings: SettingsStore,
        events: EventBuffer,
        *,
        atspi: AtspiBackend | None = None,
        portal: PortalBackend | None = None,
        apps: AppBackend | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.atspi = atspi or AtspiBackend()
        self.portal = portal or PortalBackend(settings.paths)
        self.apps = apps or AppBackend()
        self.policy = policy or Policy()

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            if method == "action" and "button" in args:
                raise InvalidRequest("button is valid only for a coordinates click")
            if method == "coordinates" and "action" in args:
                raise InvalidRequest("args.action is valid only for an AT-SPI action click")
        settings = self.settings.get()
        self.events.resize(settings.max_events)

        request_event = self.events.emit(
            "command.started",
            data={"action": action, "args": self._audit_args(action, args)},
        )
        started = time.monotonic()
        try:
            result = self._execute(action, args, settings)
        except BridgeError as exc:
            self.events.emit(
                "command.failed",
                level="warning" if exc.status < 500 else "error",
                data={
                    "action": action,
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
                "requestEventId": request_event["id"],
                "durationMs": round((time.monotonic() - started) * 1000, 2),
                "result": self._audit_result(action, result),
            },
        )
        return result

    def _execute(self, action: str, args: dict[str, Any], settings: Settings) -> dict[str, Any]:
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
            return self.stop_all(source="api")

        if action == "list_apps":
            self.policy.authorize(action, settings)
            return self.atspi.list_apps()
        if action == "snapshot":
            self.policy.authorize(action, settings)
            ref = self._ref(args)
            return self.atspi.snapshot(
                ref,
                depth=_int_arg(args, "depth", 8, minimum=0, maximum=30),
                max_nodes=_int_arg(args, "maxNodes", 1000, minimum=1, maximum=5000),
                include_text=_bool_arg(args, "includeText", True),
                redact_protected_text=settings.redact_protected_text,
            )
        if action == "screenshot":
            self.policy.authorize(action, settings)
            return self.portal.screenshot(
                interactive=_bool_arg(args, "interactive", False),
                include_base64=_bool_arg(args, "includeBase64", False),
            )
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
                requested_action = args.get("action", 0)
                if not isinstance(requested_action, (str, int)) or type(requested_action) is bool:
                    raise InvalidRequest("args.action must be an action name or integer index")
                if isinstance(requested_action, str) and len(requested_action) > 500:
                    raise InvalidRequest("args.action is too long")
                return self.atspi.invoke(ref, action=requested_action)
            if action == "fill":
                return self.atspi.fill(ref, args.get("text"))
            if action == "focus":
                return self.atspi.focus(ref)
            return self._semantic_click(ref, args, settings)

        if action == "start_remote_desktop":
            self.policy.authorize(action, settings)
            result = self.portal.start_remote_desktop(persist=settings.persist_portal_session)
            self.events.emit("portal.session.started", data=result)
            return result
        if action == "stop_remote_desktop":
            self.policy.authorize(action, settings)
            result = self.portal.stop_remote_desktop()
            self.events.emit("portal.session.stopped")
            return result
        if action == "pointer_move":
            self.policy.authorize(action, settings)
            return self.portal.pointer_move(args.get("x"), args.get("y"))
        if action == "pointer_click":
            self.policy.authorize(action, settings)
            return self.portal.pointer_click(
                button=args.get("button", "left"),
                x=args.get("x"),
                y=args.get("y"),
            )
        if action == "scroll":
            self.policy.authorize(action, settings)
            return self.portal.scroll(args.get("dx", 0.0), args.get("dy", 0.0))
        if action == "key":
            self.policy.authorize(action, settings)
            return self.portal.key(args.get("key"), event=args.get("event", "tap"))
        if action == "type_text":
            self.policy.authorize(action, settings)
            return self.portal.type_text(
                args.get("text"),
                interval_ms=_int_arg(args, "intervalMs", 10, minimum=0, maximum=1000),
            )

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
        if method not in {"action", "coordinates"}:
            raise InvalidRequest("method must be action or coordinates")
        if method == "action":
            requested_action = args.get("action", 0)
            if not isinstance(requested_action, (str, int)) or type(requested_action) is bool:
                raise InvalidRequest("args.action must be an action name or integer index")
            if isinstance(requested_action, str) and len(requested_action) > 500:
                raise InvalidRequest("args.action is too long")
            result = self.atspi.invoke(ref, action=requested_action)
            result["method"] = "atspi-action"
            return result

        # Coordinates are global desktop control even when derived from a scoped
        # semantic element, so require ALL DESKTOP independently.
        self.policy.authorize("pointer_click", settings)
        _identity, bounds = self.atspi.bounds_for_ref(ref)
        x = bounds["x"] + bounds["width"] / 2
        y = bounds["y"] + bounds["height"] / 2
        result = self.portal.pointer_click(button=args.get("button", "left"), x=x, y=y)
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

    def _require_control_mode(self, action: str, settings: Settings) -> None:
        # This check happens before resolving an AT-SPI reference, so Off/Observe
        # callers cannot probe whether a target reference exists.
        if LEVEL_BY_NAME[settings.access_mode] < AccessLevel.CONTROL:
            self.policy.authorize(action, settings, app=None)

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
            },
            "actions": {
                "always": ["ping", "capabilities", "get_state", "stop_all"],
                "observe": ["list_apps", "snapshot", "screenshot", "list_launchers"],
                "scopedControl": sorted(SCOPED_CONTROL_ACTIONS),
                "allDesktop": sorted(GLOBAL_CONTROL_ACTIONS),
            },
        }

    def state(self, settings: Settings | None = None) -> dict[str, Any]:
        settings = settings or self.settings.get()
        return {
            "version": __version__,
            "accessMode": settings.access_mode,
            "allowedApps": list(settings.allowed_apps or []),
            "features": {
                "screenshots": settings.allow_screenshots,
                "portalInput": settings.allow_portal_input,
                "launchApps": settings.allow_launch_apps,
                "persistPortalSession": settings.persist_portal_session,
                "redactProtectedText": settings.redact_protected_text,
            },
            "remoteDesktop": self.portal.status(),
            "latestEventId": self.events.latest_id(),
            "settingsError": self.settings.last_error,
        }

    def stop_all(self, *, source: str) -> dict[str, Any]:
        portal_error: str | None = None
        try:
            self.portal.stop_remote_desktop()
        except Exception as exc:  # stopping must remain best-effort and fail closed
            portal_error = type(exc).__name__
        updated, settings_error = self.settings.force_off()
        self.events.emit(
            "security.stop_all",
            level="warning",
            data={
                "source": source,
                "portalError": portal_error,
                "settingsPersistenceError": settings_error,
            },
        )
        return {
            "stopped": True,
            "accessMode": updated.access_mode,
            "portalError": portal_error,
            "settingsPersisted": settings_error is None,
        }

    def enforce_current_policy(self) -> None:
        """Close a still-active portal session as soon as ALL mode is revoked."""

        settings = self.settings.get()
        control_in_progress = getattr(self.portal, "control_in_progress", self.portal.active)
        if control_in_progress and (
            settings.access_mode != "all" or not settings.allow_portal_input
        ):
            self.portal.stop_remote_desktop()
            self.events.emit(
                "portal.session.revoked",
                level="warning",
                data={"reason": "settings_changed", "accessMode": settings.access_mode},
            )

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
        return _sanitize_for_audit(result)

    def shutdown(self) -> None:
        self.portal.shutdown()
