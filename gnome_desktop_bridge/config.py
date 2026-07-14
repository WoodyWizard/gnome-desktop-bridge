"""Persistent local configuration and bearer-token management."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .errors import InvalidRequest

APP_ID = "gnome-desktop-bridge"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18766


def _xdg_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else fallback


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    runtime_dir: Path
    settings_file: Path
    token_file: Path
    screenshots_dir: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        home = Path.home()
        config_dir = _xdg_path("XDG_CONFIG_HOME", home / ".config") / APP_ID
        data_dir = _xdg_path("XDG_DATA_HOME", home / ".local" / "share") / APP_ID
        runtime_root = _xdg_path("XDG_RUNTIME_DIR", data_dir / "run")
        runtime_dir = runtime_root / APP_ID if runtime_root != data_dir / "run" else runtime_root
        return cls(
            config_dir=config_dir,
            data_dir=data_dir,
            runtime_dir=runtime_dir,
            settings_file=config_dir / "settings.json",
            token_file=data_dir / "token",
            screenshots_dir=data_dir / "screenshots",
        )

    def ensure(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.runtime_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _tighten_directory(directory)


@dataclass(slots=True)
class Settings:
    """Human-controlled policy. The daemon reloads this file automatically."""

    version: int = 1
    access_mode: str = "off"
    allowed_apps: list[str] | None = None
    allow_screenshots: bool = True
    allow_portal_input: bool = False
    allow_launch_apps: bool = False
    persist_portal_session: bool = False
    redact_protected_text: bool = True
    max_events: int = 2000
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        if self.allowed_apps is None:
            self.allowed_apps = []
        self.validate()

    def validate(self) -> None:
        if self.version != 1:
            raise InvalidRequest(f"Unsupported settings version: {self.version}")
        if self.access_mode not in {"off", "observe", "control", "all"}:
            raise InvalidRequest(f"Unknown access mode: {self.access_mode}")
        if not isinstance(self.allowed_apps, list) or not all(
            isinstance(item, str) and item.strip() for item in self.allowed_apps
        ):
            raise InvalidRequest("allowedApps must contain non-empty strings")
        if len(self.allowed_apps) > 1000 or any(len(item) > 500 for item in self.allowed_apps):
            raise InvalidRequest("allowedApps is too large (1000 patterns, 500 characters each)")
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise InvalidRequest("The bridge may bind only to a loopback address")
        if not 1024 <= self.port <= 65535:
            raise InvalidRequest("port must be between 1024 and 65535")
        if not 100 <= self.max_events <= 20_000:
            raise InvalidRequest("maxEvents must be between 100 and 20000")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "accessMode": self.access_mode,
            "allowedApps": list(self.allowed_apps or []),
            "allowScreenshots": self.allow_screenshots,
            "allowPortalInput": self.allow_portal_input,
            "allowLaunchApps": self.allow_launch_apps,
            "persistPortalSession": self.persist_portal_session,
            "redactProtectedText": self.redact_protected_text,
            "maxEvents": self.max_events,
            "host": self.host,
            "port": self.port,
        }

    @classmethod
    def from_json_dict(cls, raw: dict[str, Any]) -> "Settings":
        if not isinstance(raw, dict):
            raise InvalidRequest("settings.json must contain a JSON object")
        known = {
            "version": "version",
            "accessMode": "access_mode",
            "allowedApps": "allowed_apps",
            "allowScreenshots": "allow_screenshots",
            "allowPortalInput": "allow_portal_input",
            "allowLaunchApps": "allow_launch_apps",
            "persistPortalSession": "persist_portal_session",
            "redactProtectedText": "redact_protected_text",
            "maxEvents": "max_events",
            "host": "host",
            "port": "port",
        }
        unexpected = sorted(set(raw) - set(known))
        if unexpected:
            raise InvalidRequest(
                "Unknown settings keys",
                details={"keys": unexpected},
            )
        kwargs = {known[key]: value for key, value in raw.items()}
        _validate_field_types(kwargs)
        return cls(**kwargs)


def _validate_field_types(values: dict[str, Any]) -> None:
    bool_fields = {
        "allow_screenshots",
        "allow_portal_input",
        "allow_launch_apps",
        "persist_portal_session",
        "redact_protected_text",
    }
    for name in bool_fields & values.keys():
        if type(values[name]) is not bool:
            raise InvalidRequest(f"{name} must be a boolean")
    for name in {"version", "max_events", "port"} & values.keys():
        if type(values[name]) is not int:
            raise InvalidRequest(f"{name} must be an integer")
    for name in {"access_mode", "host"} & values.keys():
        if not isinstance(values[name], str):
            raise InvalidRequest(f"{name} must be a string")


def _tighten_directory(path: Path) -> None:
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & 0o077:
            path.chmod(0o700)
    except OSError:
        pass


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _tighten_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_settings(paths: AppPaths | None = None) -> Settings:
    paths = paths or AppPaths.discover()
    paths.ensure()
    if not paths.settings_file.exists():
        if paths.settings_file.is_symlink():
            raise InvalidRequest("The settings file may not be a symbolic link")
        settings = Settings()
        save_settings(settings, paths)
        return settings
    if paths.settings_file.is_symlink():
        raise InvalidRequest("The settings file may not be a symbolic link")
    try:
        raw = json.loads(_read_private_settings_file(paths.settings_file))
    except json.JSONDecodeError as exc:
        raise InvalidRequest(f"Cannot read settings: {exc}") from exc
    return Settings.from_json_dict(raw)


def _read_private_settings_file(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InvalidRequest(f"Cannot inspect settings: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise InvalidRequest("settings.json must be a user-owned regular file")
    if metadata.st_size > 1024 * 1024:
        raise InvalidRequest("settings.json exceeds the 1 MiB safety limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
            raise InvalidRequest("The opened settings file is not a user-owned regular file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise InvalidRequest("settings.json changed while it was being opened")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            content = handle.read(1024 * 1024 + 1)
    except (OSError, UnicodeError) as exc:
        raise InvalidRequest(f"Cannot read settings: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(content.encode("utf-8")) > 1024 * 1024:
        raise InvalidRequest("settings.json exceeds the 1 MiB safety limit")
    return content


def save_settings(settings: Settings, paths: AppPaths | None = None) -> None:
    settings.validate()
    paths = paths or AppPaths.discover()
    paths.ensure()
    content = json.dumps(settings.to_json_dict(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write(paths.settings_file, content)


def load_or_create_token(paths: AppPaths | None = None) -> str:
    """Return the stable local token, creating it exactly once when absent."""

    paths = paths or AppPaths.discover()
    paths.ensure()
    if paths.token_file.exists() or paths.token_file.is_symlink():
        return _read_token(paths.token_file)
    token = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(paths.token_file, flags, 0o600)
    except FileExistsError:
        # Another local bridge process won the first-run race. Its value is the
        # canonical token; never replace it with this process's candidate.
        return _read_token(paths.token_file)
    raw_fd = fd
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            raw_fd = -1
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if raw_fd >= 0:
            os.close(raw_fd)
        try:
            paths.token_file.unlink()
        except OSError:
            pass
        raise
    return token


def _read_token(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InvalidRequest(f"Cannot inspect the token file: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InvalidRequest("The token path must be a regular file, not a symlink")
    if metadata.st_uid != os.getuid():
        raise InvalidRequest("The token file must be owned by the current user")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
            raise InvalidRequest("The opened token is not a user-owned regular file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise InvalidRequest("The token file changed while it was being opened")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            fd = -1
            token = handle.read(256).strip()
    except (OSError, UnicodeError) as exc:
        raise InvalidRequest(f"Cannot read the token file: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(token) != 64:
        raise InvalidRequest("The token file is invalid; expected 64 hex characters")
    try:
        bytes.fromhex(token)
    except ValueError as exc:
        raise InvalidRequest("The token file is not hexadecimal") from exc
    return token


class SettingsStore:
    """Thread-safe settings cache that notices edits made by the control center."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.discover()
        self._lock = threading.RLock()
        self._last_error: str | None = None
        try:
            self._settings = load_settings(self.paths)
        except InvalidRequest as exc:
            # Invalid policy must never preserve a previously dangerous mode.
            # Keep the broken file for diagnosis, but operate in Off in memory.
            self._settings = Settings()
            self._last_error = str(exc)
        self._mtime_ns = self._stat_mtime()

    def _stat_mtime(self) -> int:
        try:
            return self.paths.settings_file.stat().st_mtime_ns
        except OSError:
            return -1

    def get(self) -> Settings:
        with self._lock:
            current_mtime = self._stat_mtime()
            if current_mtime != self._mtime_ns:
                try:
                    self._settings = load_settings(self.paths)
                    self._last_error = None
                except InvalidRequest as exc:
                    self._settings = Settings()
                    self._last_error = str(exc)
                self._mtime_ns = current_mtime
            return Settings(**asdict(self._settings))

    @property
    def last_error(self) -> str | None:
        with self._lock:
            self.get()
            return self._last_error

    def update(self, **changes: Any) -> Settings:
        with self._lock:
            current = asdict(self.get())
            valid_names = {item.name for item in fields(Settings)}
            unknown = sorted(set(changes) - valid_names)
            if unknown:
                raise InvalidRequest("Unknown settings fields", details={"fields": unknown})
            current.update(changes)
            updated = Settings(**current)
            save_settings(updated, self.paths)
            self._settings = updated
            self._last_error = None
            self._mtime_ns = self._stat_mtime()
            return Settings(**asdict(updated))

    def force_off(self) -> tuple[Settings, str | None]:
        """Revoke access in memory even when persistence is temporarily broken."""

        with self._lock:
            current = asdict(self.get())
            current.update(access_mode="off", allow_portal_input=False)
            updated = Settings(**current)
            persistence_error: str | None = None
            try:
                save_settings(updated, self.paths)
            except Exception as exc:  # fail-closed state must survive the write failure
                persistence_error = f"{type(exc).__name__}: {exc}"
            self._settings = updated
            self._last_error = persistence_error
            # Pin the cache to the current on-disk generation. It will reload
            # only after a later external edit changes the mtime.
            self._mtime_ns = self._stat_mtime()
            return Settings(**asdict(updated)), persistence_error
