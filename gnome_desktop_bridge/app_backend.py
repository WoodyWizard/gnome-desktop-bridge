"""Application discovery and launching through Gio desktop entries."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid
from typing import Any

from .errors import BackendUnavailable, InvalidRequest, NotFound
from .policy import AppIdentity


class AppBackend:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gio: Any | None = None

    def _ensure(self) -> Any:
        if self._gio is not None:
            return self._gio
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except Exception as exc:
            raise BackendUnavailable("gio", "Gio application metadata is unavailable") from exc
        self._gio = Gio
        return Gio

    @staticmethod
    def _safe(callable_value: Any, default: Any = None) -> Any:
        try:
            return callable_value()
        except Exception:
            return default

    @staticmethod
    def _bounded(value: Any, limit: int) -> str:
        rendered = str(value or "")
        return rendered if len(rendered) <= limit else rendered[:limit] + "…[TRUNCATED]"

    def _serialize(self, app: Any) -> dict[str, Any]:
        return {
            "desktopId": self._bounded(self._safe(app.get_id, ""), 1000),
            "name": self._bounded(
                self._safe(app.get_display_name, "") or self._safe(app.get_name, ""),
                2000,
            ),
            "description": self._bounded(self._safe(app.get_description, ""), 4000),
            "executable": self._bounded(self._safe(app.get_executable, ""), 2000),
            "commandline": self._bounded(self._safe(app.get_commandline, ""), 4000),
            "supportsFiles": bool(self._safe(app.supports_files, False)),
            "supportsUris": bool(self._safe(app.supports_uris, False)),
        }

    def list_launchers(self, *, query: str = "", limit: int = 300) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise InvalidRequest("limit must be between 1 and 1000")
        Gio = self._ensure()
        query_folded = query.casefold().strip()
        with self._lock:
            launchers: list[dict[str, Any]] = []
            for app in Gio.AppInfo.get_all():
                if not self._safe(app.should_show, True):
                    continue
                serialized = self._serialize(app)
                haystack = " ".join(
                    str(serialized[key])
                    for key in ("desktopId", "name", "description", "executable")
                ).casefold()
                if query_folded and query_folded not in haystack:
                    continue
                launchers.append(serialized)
            launchers.sort(key=lambda item: (item["name"].casefold(), item["desktopId"]))
            return {"launchers": launchers[:limit], "truncated": len(launchers) > limit}

    def resolve(self, desktop_id: str) -> tuple[Any, AppIdentity]:
        if not isinstance(desktop_id, str) or not desktop_id.strip():
            raise InvalidRequest("desktopId must be a non-empty string")
        if len(desktop_id) > 1000:
            raise InvalidRequest("desktopId is too long")
        Gio = self._ensure()
        wanted = desktop_id.strip()
        with self._lock:
            app = Gio.DesktopAppInfo.new(wanted)
            if app is None:
                # Allow an exact, case-insensitive display name as a convenience,
                # but never fuzzy-match a launch request.
                matches = [
                    candidate
                    for candidate in Gio.AppInfo.get_all()
                    if str(self._safe(candidate.get_display_name, "")).casefold()
                    == wanted.casefold()
                ]
                if len(matches) == 1:
                    app = matches[0]
            if app is None:
                raise NotFound("Application launcher not found", details={"desktopId": wanted})
            app_id = self._safe(app.get_id, "") or wanted
            name = self._safe(app.get_display_name, "") or self._safe(app.get_name, "") or ""
            return app, AppIdentity(
                name=self._bounded(name, 2000),
                app_id=self._bounded(app_id, 1000),
            )

    def launch_resolved(self, app: Any, identity: AppIdentity) -> dict[str, Any]:
        with self._lock:
            systemd_run = shutil.which("systemd-run")
            gtk_launch = shutil.which("gtk-launch")
            if systemd_run and gtk_launch and identity.app_id:
                # Ask the user service manager to launch the application in a
                # fresh transient unit. A directly spawned child would inherit
                # this daemon's systemd sandbox and could not work normally.
                unit = f"gnome-desktop-bridge-launch-{uuid.uuid4().hex}"
                try:
                    completed = subprocess.run(
                        [
                            systemd_run,
                            "--user",
                            "--collect",
                            "--quiet",
                            "--property=ExitType=cgroup",
                            f"--unit={unit}",
                            "--",
                            gtk_launch,
                            identity.app_id,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise BackendUnavailable("gio", "Cannot submit the application launch") from exc
                if completed.returncode != 0:
                    message = (completed.stderr or completed.stdout).strip()
                    raise BackendUnavailable(
                        "gio",
                        f"The user service manager rejected the launch: {message or 'unknown error'}",
                    )
                return {
                    "launched": True,
                    "app": identity.as_dict(),
                    "backend": "systemd-run+gtk-launch",
                }

            if os.environ.get("INVOCATION_ID"):
                raise BackendUnavailable(
                    "gio",
                    "A safe systemd-run + gtk-launch backend is required when the daemon "
                    "runs inside its hardened user service.",
                )

            try:
                result = app.launch([], None)
            except Exception as exc:
                raise BackendUnavailable(
                    "gio",
                    f"Application launch failed: {identity.name or identity.app_id}",
                ) from exc
            if result is False:
                raise BackendUnavailable("gio", "The desktop rejected the launch request")
            return {"launched": True, "app": identity.as_dict(), "backend": "Gio.AppInfo"}
