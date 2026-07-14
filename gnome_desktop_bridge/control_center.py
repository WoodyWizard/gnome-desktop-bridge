"""Native GTK/libadwaita control center for human-owned permissions."""

from __future__ import annotations

import threading
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .cli import BridgeClient  # noqa: E402
from .config import AppPaths, SettingsStore, load_or_create_token  # noqa: E402

APP_ID = "io.github.local.GnomeDesktopBridge"
MODES = ["off", "observe", "control", "all"]


class ControlCenter(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.paths = AppPaths.discover()
        self.store = SettingsStore(self.paths)
        self.settings = self.store.get()
        self.window: Adw.PreferencesWindow | None = None

    def do_activate(self) -> None:
        if self.window is not None:
            self.window.present()
            return
        self.window = Adw.PreferencesWindow(application=self)
        self.window.set_title("GNOME Desktop Bridge")
        self.window.set_default_size(720, 720)

        page = Adw.PreferencesPage(
            title="Desktop Bridge",
            icon_name="preferences-system-symbolic",
        )
        self.window.add(page)

        access_group = Adw.PreferencesGroup(
            title="Access",
            description="Permissions are stored locally and remain under your control.",
        )
        page.add(access_group)

        model = Gtk.StringList.new(
            ["Off", "Observe", "Control selected apps", "ALL DESKTOP (dangerous)"]
        )
        self.mode_row = Adw.ComboRow(
            title="Access mode",
            subtitle="ALL DESKTOP removes the per-application boundary.",
            model=model,
        )
        self.mode_row.set_selected(MODES.index(self.settings.access_mode))
        access_group.add(self.mode_row)

        self.allowed_apps_row = Adw.EntryRow(title="Allowed applications (comma-separated globs)")
        self.allowed_apps_row.set_text(", ".join(self.settings.allowed_apps or []))
        access_group.add(self.allowed_apps_row)

        feature_group = Adw.PreferencesGroup(
            title="Feature gates",
            description="A mode and its corresponding feature gate must both permit an action.",
        )
        page.add(feature_group)
        self.screenshot_row = self._switch_row(
            feature_group,
            "Screenshots",
            "Allow the XDG Screenshot portal in Observe mode or higher.",
            self.settings.allow_screenshots,
        )
        self.launch_row = self._switch_row(
            feature_group,
            "Launch applications",
            "Allow launching desktop entries in Control mode or higher.",
            self.settings.allow_launch_apps,
        )
        self.portal_input_row = self._switch_row(
            feature_group,
            "Global pointer and keyboard",
            "Requires ALL DESKTOP and an additional GNOME permission dialog.",
            self.settings.allow_portal_input,
        )
        self.persist_portal_row = self._switch_row(
            feature_group,
            "Request persistent portal permission",
            "Ask GNOME to restore permission when supported; consent remains revocable.",
            self.settings.persist_portal_session,
        )
        self.redact_row = self._switch_row(
            feature_group,
            "Redact protected text",
            "Replace password-field contents with [REDACTED] in semantic snapshots.",
            self.settings.redact_protected_text,
        )

        action_group = Adw.PreferencesGroup(title="Apply and session control")
        page.add(action_group)
        apply_row = Adw.ActionRow(
            title="Save permission settings",
            subtitle="The daemon reloads them automatically.",
        )
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.set_valign(Gtk.Align.CENTER)
        apply_button.connect("clicked", self._on_apply)
        apply_row.add_suffix(apply_button)
        action_group.add(apply_row)

        remote_row = Adw.ActionRow(
            title="Remote Desktop portal session",
            subtitle="GNOME will show a screen-sharing and input confirmation dialog.",
        )
        self.start_button = Gtk.Button(label="Start session")
        self.start_button.set_valign(Gtk.Align.CENTER)
        self.start_button.connect("clicked", self._on_start_session)
        remote_row.add_suffix(self.start_button)
        action_group.add(remote_row)

        stop_row = Adw.ActionRow(
            title="Emergency stop",
            subtitle="Close portal control, disable global input, and switch the bridge to Off.",
        )
        self.stop_button = Gtk.Button(label="STOP ALL")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.set_valign(Gtk.Align.CENTER)
        self.stop_button.connect("clicked", self._on_stop_all)
        stop_row.add_suffix(self.stop_button)
        action_group.add(stop_row)

        status_group = Adw.PreferencesGroup(title="Runtime status")
        page.add(status_group)
        self.daemon_row = Adw.ActionRow(title="Daemon", subtitle="Checking…")
        status_group.add(self.daemon_row)
        self.remote_status_row = Adw.ActionRow(title="Portal control", subtitle="Checking…")
        status_group.add(self.remote_status_row)
        refresh_row = Adw.ActionRow(title="Refresh status")
        refresh_button = Gtk.Button(label="Refresh")
        refresh_button.set_valign(Gtk.Align.CENTER)
        refresh_button.connect("clicked", lambda *_args: self._refresh_status())
        refresh_row.add_suffix(refresh_button)
        status_group.add(refresh_row)

        security_group = Adw.PreferencesGroup(
            title="Authentication",
            description="The token is stable across reboots until you rotate or delete it.",
        )
        page.add(security_group)
        token = load_or_create_token(self.paths)
        token_row = Adw.ActionRow(
            title="Local bearer token",
            subtitle=f"{token[:8]}…{token[-4:]} · {self.paths.token_file}",
        )
        copy_button = Gtk.Button(label="Copy")
        copy_button.set_valign(Gtk.Align.CENTER)
        copy_button.connect("clicked", self._on_copy_token)
        token_row.add_suffix(copy_button)
        security_group.add(token_row)

        self.window.present()
        self._refresh_status()

    @staticmethod
    def _switch_row(
        group: Adw.PreferencesGroup,
        title: str,
        subtitle: str,
        active: bool,
    ) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
        group.add(row)
        return row

    def _read_proposed(self) -> dict[str, Any]:
        allowed = [
            item.strip() for item in self.allowed_apps_row.get_text().split(",") if item.strip()
        ]
        return {
            "access_mode": MODES[self.mode_row.get_selected()],
            "allowed_apps": allowed,
            "allow_screenshots": self.screenshot_row.get_active(),
            "allow_launch_apps": self.launch_row.get_active(),
            "allow_portal_input": self.portal_input_row.get_active(),
            "persist_portal_session": self.persist_portal_row.get_active(),
            "redact_protected_text": self.redact_row.get_active(),
        }

    def _on_apply(self, _button: Gtk.Button) -> None:
        proposed = self._read_proposed()
        if proposed["access_mode"] == "all":
            dialog = Adw.AlertDialog(
                heading="Enable ALL DESKTOP?",
                body=(
                    "This removes the application allowlist. If global pointer and keyboard is "
                    "enabled, an authorized client can interact with anything visible in the "
                    "shared desktop session. GNOME will still ask before a portal session starts."
                ),
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("enable", "Enable ALL DESKTOP")
            dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.choose(self.window, None, self._on_all_confirmation, proposed)
            return
        self._save_proposed(proposed)

    def _on_all_confirmation(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        proposed: dict[str, Any],
    ) -> None:
        try:
            response = dialog.choose_finish(result)
        except GLib.Error as exc:
            self._toast(f"Confirmation failed: {exc.message}")
            return
        if response == "enable":
            self._save_proposed(proposed)

    def _save_proposed(self, proposed: dict[str, Any]) -> None:
        if proposed["access_mode"] != "all":
            proposed["allow_portal_input"] = False
            self.portal_input_row.set_active(False)
        try:
            self.settings = self.store.update(**proposed)
        except Exception as exc:
            self._toast(f"Could not save settings: {exc}")
            if proposed["access_mode"] == "off":
                self._run_async(
                    lambda: BridgeClient().command("stop_all"),
                    silent_errors=True,
                )
            return
        self._toast(f"Access mode saved: {self.settings.access_mode}")
        if self.settings.access_mode == "off":
            self._run_async(lambda: BridgeClient().command("stop_all"), silent_errors=True)
        self._refresh_status()

    def _on_start_session(self, _button: Gtk.Button) -> None:
        # Use on-disk settings rather than unsaved switch states.
        current = self.store.get()
        if current.access_mode != "all" or not current.allow_portal_input:
            self._toast("Save ALL DESKTOP with Global pointer and keyboard enabled first")
            return
        self.start_button.set_sensitive(False)
        self._run_async(
            lambda: BridgeClient().command("start_remote_desktop"),
            on_success=lambda _result: self._session_started(),
            on_finish=lambda: self.start_button.set_sensitive(True),
        )

    def _session_started(self) -> None:
        self._toast("Remote Desktop portal session is active")
        self._refresh_status()

    def _on_stop_all(self, _button: Gtk.Button) -> None:
        self.stop_button.set_sensitive(False)
        self._run_async(
            lambda: BridgeClient().command("stop_all"),
            on_success=lambda _result: self._stopped(),
            on_finish=lambda: self.stop_button.set_sensitive(True),
        )

    def _stopped(self) -> None:
        self.settings = self.store.get()
        self.mode_row.set_selected(MODES.index("off"))
        self.portal_input_row.set_active(False)
        self._toast("All desktop control stopped")
        self._refresh_status()

    def _on_copy_token(self, _button: Gtk.Button) -> None:
        token = load_or_create_token(self.paths)
        if self.window is not None:
            self.window.get_clipboard().set(token)
            self._toast("Token copied to clipboard")

    def _refresh_status(self) -> None:
        self.daemon_row.set_subtitle("Checking…")
        self._run_async(
            lambda: BridgeClient().status(),
            on_success=self._show_status,
            on_error=self._show_status_error,
            silent_errors=True,
        )

    def _show_status(self, result: dict[str, Any]) -> None:
        mode = result.get("accessMode", "unknown")
        latest = result.get("latestEventId", 0)
        settings_error = result.get("settingsError")
        if settings_error:
            self.daemon_row.set_subtitle(
                f"Connected · FAIL-CLOSED in Off · settings error: {settings_error}"
            )
        else:
            self.daemon_row.set_subtitle(f"Connected · mode {mode} · latest event #{latest}")
        remote = result.get("remoteDesktop", {})
        if remote.get("active"):
            devices = remote.get("devices", {})
            labels = [name for name, active in devices.items() if active]
            self.remote_status_row.set_subtitle(
                f"Active · {', '.join(labels) or 'no input devices'} · "
                f"{len(remote.get('streams', []))} screen stream(s)"
            )
        else:
            self.remote_status_row.set_subtitle("Inactive")

    def _show_status_error(self, exc: Exception) -> None:
        self.daemon_row.set_subtitle(f"Not connected · {exc}")
        self.remote_status_row.set_subtitle("Unknown")

    def _run_async(
        self,
        work: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_finish: Callable[[], None] | None = None,
        silent_errors: bool = False,
    ) -> None:
        def runner() -> None:
            try:
                value = work()
                GLib.idle_add(self._async_success, value, on_success, on_finish)
            except Exception as exc:
                GLib.idle_add(
                    self._async_error,
                    exc,
                    on_error,
                    on_finish,
                    silent_errors,
                )

        threading.Thread(target=runner, daemon=True, name="bridge-control-task").start()

    @staticmethod
    def _async_success(
        value: Any,
        callback: Callable[[Any], None] | None,
        finish: Callable[[], None] | None,
    ) -> bool:
        if callback is not None:
            callback(value)
        if finish is not None:
            finish()
        return GLib.SOURCE_REMOVE

    def _async_error(
        self,
        exc: Exception,
        callback: Callable[[Exception], None] | None,
        finish: Callable[[], None] | None,
        silent: bool,
    ) -> bool:
        if callback is not None:
            callback(exc)
        elif not silent:
            self._toast(str(exc))
        if finish is not None:
            finish()
        return GLib.SOURCE_REMOVE

    def _toast(self, message: str) -> None:
        if self.window is not None:
            self.window.add_toast(Adw.Toast(title=message, timeout=4))


def main(argv: list[str] | None = None) -> int:
    app = ControlCenter()
    return app.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
