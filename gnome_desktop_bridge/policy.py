"""Central authorization policy for every desktop command."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Iterable

from .config import Settings
from .errors import AccessDenied, InvalidRequest


class AccessLevel(IntEnum):
    OFF = 0
    OBSERVE = 1
    CONTROL = 2
    ALL = 3


LEVEL_BY_NAME = {
    "off": AccessLevel.OFF,
    "observe": AccessLevel.OBSERVE,
    "control": AccessLevel.CONTROL,
    "all": AccessLevel.ALL,
}

ALWAYS_ACTIONS = {"ping", "capabilities", "get_state", "stop_all"}
OBSERVE_ACTIONS = {"list_apps", "snapshot", "screenshot", "list_launchers"}
SCOPED_CONTROL_ACTIONS = {"invoke", "fill", "focus", "click", "launch_app"}
GLOBAL_CONTROL_ACTIONS = {
    "start_remote_desktop",
    "stop_remote_desktop",
    "pointer_move",
    "pointer_click",
    "scroll",
    "key",
    "type_text",
}
KNOWN_ACTIONS = ALWAYS_ACTIONS | OBSERVE_ACTIONS | SCOPED_CONTROL_ACTIONS | GLOBAL_CONTROL_ACTIONS


@dataclass(frozen=True, slots=True)
class AppIdentity:
    name: str = ""
    app_id: str = ""
    toolkit_name: str = ""
    pid: int | None = None

    def candidates(self) -> list[str]:
        values = [self.name, self.app_id, self.toolkit_name]
        if self.pid is not None:
            values.append(str(self.pid))
        return [value.casefold() for value in values if value]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "appId": self.app_id,
            "toolkitName": self.toolkit_name,
            "pid": self.pid,
        }


def app_matches(identity: AppIdentity, patterns: Iterable[str]) -> bool:
    candidates = identity.candidates()
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().casefold()
        if not pattern:
            continue
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            return True
    return False


class Policy:
    def authorize(
        self,
        action: str,
        settings: Settings,
        *,
        app: AppIdentity | None = None,
    ) -> None:
        if action not in KNOWN_ACTIONS:
            raise InvalidRequest(f"Unknown action: {action}")
        if action in ALWAYS_ACTIONS:
            return

        level = LEVEL_BY_NAME[settings.access_mode]
        if action in OBSERVE_ACTIONS:
            if level < AccessLevel.OBSERVE:
                raise AccessDenied(
                    f"{action} requires Observe mode or higher",
                    details={"requiredMode": "observe", "currentMode": settings.access_mode},
                )
            if action == "screenshot" and not settings.allow_screenshots:
                raise AccessDenied("Screenshots are disabled in the control center")
            return

        if action in SCOPED_CONTROL_ACTIONS:
            if level < AccessLevel.CONTROL:
                raise AccessDenied(
                    f"{action} requires Control mode or higher",
                    details={"requiredMode": "control", "currentMode": settings.access_mode},
                )
            if action == "launch_app" and not settings.allow_launch_apps:
                raise AccessDenied("Launching applications is disabled in the control center")
            if level == AccessLevel.ALL:
                return
            if app is None:
                raise AccessDenied("A target application is required in scoped Control mode")
            if not app_matches(app, settings.allowed_apps or []):
                raise AccessDenied(
                    "The target application is outside the Control-mode allowlist",
                    details={"app": app.as_dict(), "allowedApps": settings.allowed_apps},
                )
            return

        if action in GLOBAL_CONTROL_ACTIONS:
            if level != AccessLevel.ALL:
                raise AccessDenied(
                    f"{action} is available only in ALL DESKTOP mode",
                    details={"requiredMode": "all", "currentMode": settings.access_mode},
                )
            if action not in {"stop_remote_desktop"} and not settings.allow_portal_input:
                raise AccessDenied("Global keyboard and pointer control is disabled")
            return

        raise AccessDenied(f"Action is not permitted: {action}")
