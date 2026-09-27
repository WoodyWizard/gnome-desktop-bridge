"""Native GTK/libadwaita control center for human-owned permissions."""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .cli import BridgeClient, ClientError  # noqa: E402
from .config import (  # noqa: E402
    AppPaths,
    Settings,
    SettingsStore,
    _atomic_write,
    load_or_create_token,
)

APP_ID = "io.github.local.GnomeDesktopBridge"
CLIENT_NAME = "control-center"
SERVICE_NAME = "gnome-desktop-bridge.service"
EXTENSION_UUID = "gnome-desktop-bridge-overlay@woodywizard.github.io"
MODES = ["off", "observe", "control", "all"]
MODE_TITLES = {
    "off": "Off",
    "observe": "Observe",
    "control": "Control selected apps",
    "all": "All desktop",
}
MODE_DESCRIPTIONS = {
    "off": "Agents can only check that the bridge exists.",
    "observe": "Agents can read application interfaces and take screenshots.",
    "control": "Agents can operate the applications you allow.",
    "all": "Agents can operate every application and, with consent, the pointer and keyboard.",
}
MODE_ICONS = {
    "off": "system-shutdown-symbolic",
    "observe": "view-reveal-symbolic",
    "control": "input-mouse-symbolic",
    "all": "dialog-warning-symbolic",
}
MAX_ACTIVITY_ROWS = 300
POLL_SECONDS = 2
STREAM_RETRY_SECONDS = 2.0

CSS = """
.bridge-badge {
  min-width: 112px;
  min-height: 112px;
  border-radius: 999px;
  background-color: alpha(@view_fg_color, 0.07);
  color: alpha(@view_fg_color, 0.55);
}
.bridge-badge.mode-observe { background-color: alpha(@blue_3, 0.16); color: @blue_3; }
.bridge-badge.mode-control { background-color: alpha(@green_4, 0.16); color: @green_4; }
.bridge-badge.mode-all { background-color: alpha(@orange_4, 0.18); color: @orange_4; }
.bridge-badge.offline { background-color: alpha(@view_fg_color, 0.05); color: alpha(@view_fg_color, 0.35); }
.bridge-badge.agent {
  animation: bridge-pulse 2.4s ease-out infinite;
}
@keyframes bridge-pulse {
  0% { box-shadow: 0 0 0 0 alpha(@purple_3, 0.55); }
  70% { box-shadow: 0 0 0 26px alpha(@purple_3, 0); }
  100% { box-shadow: 0 0 0 0 alpha(@purple_3, 0); }
}
.bridge-hero-title { font-size: 20pt; font-weight: 800; }
.bridge-stop {
  background-color: @destructive_bg_color;
  color: @destructive_fg_color;
  min-height: 48px;
  padding-left: 32px;
  padding-right: 32px;
  font-weight: 800;
  font-size: 12pt;
}
.bridge-live-dot {
  min-width: 10px;
  min-height: 10px;
  border-radius: 999px;
  background-color: @purple_3;
  animation: bridge-blink 1.4s ease-in-out infinite;
}
@keyframes bridge-blink {
  0% { opacity: 1; }
  50% { opacity: 0.25; }
  100% { opacity: 1; }
}
.bridge-event-icon { border-radius: 999px; padding: 8px; background-color: alpha(@view_fg_color, 0.07); }
.bridge-event-icon.agent { background-color: alpha(@purple_3, 0.2); color: @purple_3; }
.bridge-event-icon.ok { background-color: alpha(@green_4, 0.16); color: @green_4; }
.bridge-event-icon.warning { background-color: alpha(@orange_4, 0.18); color: @orange_4; }
.bridge-event-icon.error { background-color: alpha(@red_3, 0.18); color: @red_3; }
.bridge-mode-toggles { padding: 3px; }
"""


def _since(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"


def _humanize(action: str) -> str:
    return action.replace("_", " ").capitalize()


def _event_presentation(event: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return (icon, style, title, subtitle) for the activity feed, or None."""

    kind = str(event.get("type", ""))
    data = event.get("data") or {}
    client = data.get("client")
    if kind.startswith("visual.") or kind == "command.started":
        return None
    if kind == "command.completed":
        by = f"by {client} · " if client and client != CLIENT_NAME else ""
        return (
            "object-select-symbolic",
            "ok",
            _humanize(str(data.get("action", ""))),
            f"{by}{data.get('durationMs', 0):.0f} ms",
        )
    if kind == "command.failed":
        error = data.get("error") or {}
        by = f"by {client} · " if client and client != CLIENT_NAME else ""
        return (
            "dialog-warning-symbolic",
            "warning" if event.get("level") == "warning" else "error",
            f"{_humanize(str(data.get('action', '')))} failed",
            f"{by}{error.get('message', error.get('code', ''))}",
        )
    if kind == "agent.connected":
        return ("avatar-default-symbolic", "agent", f"{data.get('name')} connected", "")
    if kind == "agent.disconnected":
        reasons = {
            "goodbye": "finished",
            "idle": "went idle",
            "stop_all": "access stopped",
            "replaced": "replaced by another agent",
        }
        return (
            "avatar-default-symbolic",
            "",
            f"{data.get('name')} disconnected",
            f"{reasons.get(data.get('reason'), data.get('reason', ''))} · "
            f"{data.get('commands', 0)} commands",
        )
    if kind == "security.stop_all":
        return ("process-stop-symbolic", "error", "Stop all", f"from {data.get('source')}")
    if kind == "security.startup_downgrade":
        return ("security-high-symbolic", "warning", "All desktop reset to Off at startup", "")
    if kind == "settings.changed":
        return (
            "emblem-system-symbolic",
            "",
            "Settings changed",
            f"mode {MODE_TITLES.get(data.get('accessMode'), data.get('accessMode'))}",
        )
    if kind.startswith("portal.session."):
        titles = {
            "started": "Remote control session started",
            "stopped": "Remote control session stopped",
            "revoked": "Remote control session revoked",
            "ended": "Remote control session ended by the desktop",
        }
        return (
            "input-mouse-symbolic",
            "warning" if kind.endswith("started") else "",
            titles.get(kind.rsplit(".", 1)[-1], kind),
            "",
        )
    if kind.startswith("daemon."):
        return ("system-run-symbolic", "", _humanize(kind.split(".", 1)[1]), "")
    return ("dialog-information-symbolic", "", kind, "")


class AppPickerDialog(Adw.Dialog):
    """Pick an installed application to add to the Control allowlist."""

    def __init__(self, on_pick: Callable[[Gio.AppInfo], None]) -> None:
        super().__init__(title="Add an application", content_width=440, content_height=560)
        self._on_pick = on_pick
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        self._search = Gtk.SearchEntry(placeholder_text="Search applications")
        search_bar = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        search_bar.append(self._search)
        self._search.set_hexpand(True)
        toolbar.add_top_bar(search_bar)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_filter_func(self._filter)
        apps = [app for app in Gio.AppInfo.get_all() if app.should_show()]
        apps.sort(key=lambda app: (app.get_display_name() or "").casefold())
        for app in apps:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(app.get_display_name() or ""),
                subtitle=GLib.markup_escape_text(app.get_id() or ""),
                activatable=True,
            )
            icon = app.get_icon()
            image = Gtk.Image.new_from_gicon(icon) if icon else Gtk.Image()
            image.set_pixel_size(32)
            row.add_prefix(image)
            row.app_info = app
            row.connect("activated", self._activated)
            self._list.append(row)
        clamp = Adw.Clamp(
            child=self._list,
            margin_top=6,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        toolbar.set_content(Gtk.ScrolledWindow(child=clamp, vexpand=True))
        self.set_child(toolbar)
        self._search.connect("search-changed", lambda *_args: self._list.invalidate_filter())

    def _filter(self, row: Gtk.ListBoxRow) -> bool:
        query = self._search.get_text().casefold().strip()
        if not query:
            return True
        app = row.app_info
        haystack = f"{app.get_display_name()} {app.get_id()} {app.get_executable()}"
        return query in haystack.casefold()

    def _activated(self, row: Adw.ActionRow) -> None:
        self._on_pick(row.app_info)
        self.close()


class BridgeWindow(Adw.ApplicationWindow):
    def __init__(self, application: "ControlCenter") -> None:
        super().__init__(application=application, title="Desktop Bridge")
        self.set_default_size(760, 820)
        self.set_size_request(360, 480)
        self.paths: AppPaths = application.paths
        self.store: SettingsStore = application.store
        self.settings: Settings = self.store.get()
        self.daemon: dict[str, Any] | None = None
        self._syncing = False
        self._closing = threading.Event()
        self._latest_event = 0
        self._activity_count = 0
        self._last_status_refresh = 0.0

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar.add_top_bar(header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner_button)
        toolbar.add_top_bar(self.banner)
        switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        toolbar.add_bottom_bar(switcher_bar)
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        breakpoint.add_setter(switcher_bar, "reveal", True)
        breakpoint.add_setter(switcher, "visible", False)
        self.add_breakpoint(breakpoint)

        self.stack.add_titled_with_icon(
            self._build_overview(), "overview", "Overview", "computer-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._build_permissions(), "permissions", "Permissions", "security-high-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._build_activity(), "activity", "Activity", "document-open-recent-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._build_overlay(), "overlay", "Animations", "starred-symbolic"
        )

        self._sync_settings()
        self._show_daemon(None)
        self.refresh_status()
        self._check_extension()
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll)
        threading.Thread(target=self._stream_loop, name="bridge-activity", daemon=True).start()
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ build

    def _build_overview(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        hero = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            margin_top=12,
        )
        self.badge = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.badge.add_css_class("bridge-badge")
        self.badge_icon = Gtk.Image(
            pixel_size=52,
            hexpand=True,
            vexpand=True,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.badge.append(self.badge_icon)
        badge_frame = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=16, margin_bottom=10)
        badge_frame.append(self.badge)
        hero.append(badge_frame)
        self.hero_title = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self.hero_title.add_css_class("bridge-hero-title")
        hero.append(self.hero_title)
        self.hero_subtitle = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self.hero_subtitle.add_css_class("dim-label")
        hero.append(self.hero_subtitle)

        self.mode_toggles = Adw.ToggleGroup(halign=Gtk.Align.CENTER, margin_top=18)
        self.mode_toggles.add_css_class("round")
        self.mode_toggles.add_css_class("bridge-mode-toggles")
        for mode in MODES:
            label = "All desktop" if mode == "all" else MODE_TITLES[mode].split()[0]
            self.mode_toggles.add(
                Adw.Toggle(name=mode, label=label, tooltip=MODE_DESCRIPTIONS[mode])
            )
        self.mode_toggles.connect("notify::active-name", self._on_mode_toggled)
        hero.append(self.mode_toggles)
        self.mode_description = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.mode_description.add_css_class("caption")
        self.mode_description.add_css_class("dim-label")
        hero.append(self.mode_description)

        hero_group = Adw.PreferencesGroup()
        hero_group.add(hero)
        page.add(hero_group)

        session_group = Adw.PreferencesGroup(title="Live session")
        self.agent_row = Adw.ActionRow(title="No agent connected")
        self.agent_avatar = Adw.Avatar(size=40, show_initials=True)
        self.agent_row.add_prefix(self.agent_avatar)
        self.agent_live = Gtk.Box(valign=Gtk.Align.CENTER, visible=False)
        self.agent_live.add_css_class("bridge-live-dot")
        self.agent_row.add_suffix(self.agent_live)
        session_group.add(self.agent_row)

        self.remote_row = Adw.ActionRow(
            title="Pointer and keyboard control",
            subtitle="Inactive",
        )
        self.remote_row.add_prefix(Gtk.Image(icon_name="input-mouse-symbolic"))
        self.remote_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.remote_button.connect("clicked", self._on_remote_button)
        self.remote_row.add_suffix(self.remote_button)
        session_group.add(self.remote_row)

        self.daemon_row = Adw.ActionRow(title="Bridge daemon", subtitle="Checking…")
        self.daemon_row.add_prefix(Gtk.Image(icon_name="system-run-symbolic"))
        self.daemon_spinner = Adw.Spinner(valign=Gtk.Align.CENTER)
        self.daemon_row.add_suffix(self.daemon_spinner)
        session_group.add(self.daemon_row)
        page.add(session_group)

        stop_group = Adw.PreferencesGroup()
        stop_button = Gtk.Button(
            label="Stop all agent access",
            halign=Gtk.Align.CENTER,
            margin_top=6,
        )
        stop_button.add_css_class("destructive-action")
        stop_button.add_css_class("pill")
        stop_button.add_css_class("bridge-stop")
        stop_button.set_tooltip_text(
            "Close remote control, switch to Off, and disconnect the agent (Ctrl+Shift+Escape)"
        )
        stop_button.connect("clicked", lambda *_args: self.stop_all())
        stop_group.add(stop_button)
        page.add(stop_group)
        return page

    def _build_permissions(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        self.apps_group = Adw.PreferencesGroup(
            title="Applications agents may control",
            description=(
                "Used in Control mode. Patterns are case-insensitive globs matched against "
                "the application name, ID, toolkit, or process ID."
            ),
        )
        add_button = Gtk.Button(
            icon_name="list-add-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Add an installed application",
        )
        add_button.add_css_class("flat")
        add_button.connect("clicked", self._on_pick_app)
        self.apps_group.set_header_suffix(add_button)
        self.pattern_entry = Adw.EntryRow(title="Add a pattern, e.g. org.mozilla.firefox")
        self.pattern_entry.set_show_apply_button(True)
        self.pattern_entry.connect("apply", self._on_pattern_apply)
        self.apps_group.add(self.pattern_entry)
        self._app_rows: list[Adw.ActionRow] = []
        page.add(self.apps_group)

        features = Adw.PreferencesGroup(
            title="Feature gates",
            description="Both the access mode and the matching gate must allow an action.",
        )
        self.feature_rows: dict[str, Adw.SwitchRow] = {}
        for field, title, subtitle in (
            ("allow_screenshots", "Screenshots", "Observe mode or higher"),
            ("allow_launch_apps", "Launch applications", "Control mode or higher"),
            (
                "allow_portal_input",
                "Global pointer and keyboard",
                "All desktop only; GNOME asks for consent when a session starts",
            ),
            (
                "persist_portal_session",
                "Remember remote control consent",
                "Ask GNOME to restore the permission next time; revocable",
            ),
            (
                "redact_protected_text",
                "Hide password fields",
                "Replace password contents with [REDACTED] in interface snapshots",
            ),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.connect("notify::active", self._on_feature_toggled, field)
            features.add(row)
            self.feature_rows[field] = row
        page.add(features)

        auth = Adw.PreferencesGroup(
            title="Authentication",
            description="Local clients authenticate with this token. It survives restarts.",
        )
        self.token_row = Adw.ActionRow(title="Bearer token")
        self.token_row.add_css_class("property")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy.set_tooltip_text("Copy token")
        copy.add_css_class("flat")
        copy.connect("clicked", self._on_copy_token)
        rotate = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        rotate.set_tooltip_text("Rotate token")
        rotate.add_css_class("flat")
        rotate.connect("clicked", self._on_rotate_token)
        self.token_row.add_suffix(copy)
        self.token_row.add_suffix(rotate)
        auth.add(self.token_row)
        self._show_token()
        page.add(auth)
        return page

    def _build_activity(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=12, margin_start=18, margin_end=18, margin_top=12, margin_bottom=6)
        title = Gtk.Label(label="Live audit log", xalign=0, hexpand=True)
        title.add_css_class("heading")
        bar.append(title)
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_args: self._clear_activity())
        bar.append(clear)
        box.append(bar)

        self.activity_stack = Gtk.Stack(vexpand=True)
        empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="No activity yet",
            description="Commands, agent connections, and permission changes appear here live.",
        )
        self.activity_stack.add_named(empty, "empty")
        self.activity_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.activity_list.add_css_class("boxed-list")
        clamp = Adw.Clamp(
            child=self.activity_list,
            maximum_size=720,
            margin_start=12,
            margin_end=12,
            margin_bottom=18,
        )
        scrolled = Gtk.ScrolledWindow(child=clamp, vexpand=True)
        self.activity_stack.add_named(scrolled, "list")
        box.append(self.activity_stack)
        return box

    def _build_overlay(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="On-screen feedback",
            description=(
                "While an agent works, the desktop shows what it is doing: a glow around "
                "the screen, an animated agent cursor, click ripples, highlighted targets, "
                "and pressed keys."
            ),
        )
        self.overlay_rows: dict[str, Adw.SwitchRow] = {}
        for field, title, subtitle in (
            ("overlay_enabled", "Show agent activity on screen", "Requires the Shell extension"),
            (
                "overlay_edge_glow",
                "Glow around the screen",
                "Violet while an agent is connected, amber while it controls input",
            ),
            (
                "overlay_show_keys",
                "Show pressed keys",
                "Lone characters are always shown as dots; shortcuts are spelled out",
            ),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.connect("notify::active", self._on_feature_toggled, field)
            group.add(row)
            self.overlay_rows[field] = row
        self.motion_row = Adw.SpinRow.new_with_range(0, 2000, 50)
        self.motion_row.set_title("Pointer glide duration")
        self.motion_row.set_subtitle(
            "Milliseconds the real pointer takes to reach a target; 0 jumps instantly"
        )
        self.motion_row.connect("notify::value", self._on_motion_changed)
        group.add(self.motion_row)
        page.add(group)

        extension = Adw.PreferencesGroup(title="GNOME Shell extension")
        self.extension_row = Adw.ActionRow(title="Desktop Bridge Overlay", subtitle="Checking…")
        self.extension_row.add_prefix(Gtk.Image(icon_name="application-x-addon-symbolic"))
        self.extension_button = Gtk.Button(label="Enable", valign=Gtk.Align.CENTER, visible=False)
        self.extension_button.add_css_class("suggested-action")
        self.extension_button.connect("clicked", self._on_enable_extension)
        self.extension_row.add_suffix(self.extension_button)
        extension.add(self.extension_row)
        page.add(extension)
        return page

    # --------------------------------------------------------------- settings

    def _sync_settings(self) -> None:
        """Show the on-disk settings without triggering change handlers."""

        settings = self.settings
        self._syncing = True
        try:
            self.mode_toggles.set_active_name(settings.access_mode)
            self.mode_description.set_label(MODE_DESCRIPTIONS[settings.access_mode])
            for field, row in {**self.feature_rows, **self.overlay_rows}.items():
                row.set_active(bool(getattr(settings, field)))
            self.feature_rows["allow_portal_input"].set_sensitive(settings.access_mode == "all")
            self.motion_row.set_value(settings.pointer_motion_ms)
            self._rebuild_app_rows(settings.allowed_apps or [])
        finally:
            self._syncing = False
        self._update_hero()

    def _save(self, **changes: Any) -> bool:
        try:
            self.settings = self.store.update(**changes)
        except Exception as exc:
            self.toast(f"Could not save settings: {exc}")
            self.settings = self.store.get()
            self._sync_settings()
            return False
        self._sync_settings()
        return True

    def _poll(self) -> bool:
        if self._closing.is_set():
            return GLib.SOURCE_REMOVE
        current = self.store.get()
        if current.to_json_dict() != self.settings.to_json_dict():
            self.settings = current
            self._sync_settings()
        # The event stream refreshes status while connected; poll otherwise.
        if self.daemon is None or time.monotonic() - self._last_status_refresh > 10:
            self.refresh_status()
        elif self.daemon.get("agent"):
            self._update_agent()
        return GLib.SOURCE_CONTINUE

    def _on_mode_toggled(self, group: Adw.ToggleGroup, _pspec: Any) -> None:
        if self._syncing:
            return
        mode = group.get_active_name()
        if mode == self.settings.access_mode:
            return
        if mode == "off":
            self.stop_all()
        elif mode == "all":
            self._confirm_all()
        else:
            if self._save(access_mode=mode, allow_portal_input=False) and mode == "control":
                if not self.settings.allowed_apps:
                    toast = Adw.Toast(
                        title="Add the applications agents may control",
                        button_label="Choose",
                        timeout=6,
                    )
                    toast.connect(
                        "button-clicked",
                        lambda *_args: self.stack.set_visible_child_name("permissions"),
                    )
                    self.toasts.add_toast(toast)

    def _confirm_all(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Allow control of the whole desktop?",
            body=(
                "Agents will be able to operate every application, not only the ones you "
                "allowed. This mode resets to Off whenever the bridge restarts."
            ),
        )
        check = Gtk.CheckButton(label="Also allow global pointer and keyboard input")
        check.set_active(self.settings.allow_portal_input)
        dialog.set_extra_child(check)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("enable", "Allow All Desktop")
        dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def chosen(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            if source.choose_finish(result) == "enable":
                self._save(access_mode="all", allow_portal_input=check.get_active())
            else:
                self._sync_settings()

        dialog.choose(self, None, chosen)

    def _on_feature_toggled(self, row: Adw.SwitchRow, _pspec: Any, field: str) -> None:
        if self._syncing or row.get_active() == getattr(self.settings, field):
            return
        self._save(**{field: row.get_active()})

    def _on_motion_changed(self, row: Adw.SpinRow, _pspec: Any) -> None:
        value = int(row.get_value())
        if self._syncing or value == self.settings.pointer_motion_ms:
            return
        self._save(pointer_motion_ms=value)

    # --------------------------------------------------------- allowed apps

    def _rebuild_app_rows(self, patterns: list[str]) -> None:
        for row in self._app_rows:
            self.apps_group.remove(row)
        self._app_rows = []
        for pattern in patterns:
            row = Adw.ActionRow(title=GLib.markup_escape_text(pattern))
            row.add_prefix(Gtk.Image(icon_name="application-x-executable-symbolic"))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text("Remove")
            remove.connect("clicked", self._on_remove_pattern, pattern)
            row.add_suffix(remove)
            self.apps_group.add(row)
            self._app_rows.append(row)

    def _add_patterns(self, patterns: list[str]) -> None:
        current = list(self.settings.allowed_apps or [])
        added = [item for item in dict.fromkeys(patterns) if item and item not in current]
        if not added:
            self.toast("Already allowed")
            return
        if self._save(allowed_apps=current + added):
            self.toast(f"Allowed {', '.join(added)}")

    def _on_pattern_apply(self, entry: Adw.EntryRow) -> None:
        patterns = [item.strip() for item in entry.get_text().split(",") if item.strip()]
        entry.set_text("")
        self._add_patterns(patterns)

    def _on_remove_pattern(self, _button: Gtk.Button, pattern: str) -> None:
        remaining = [item for item in self.settings.allowed_apps or [] if item != pattern]
        self._save(allowed_apps=remaining)

    def _on_pick_app(self, _button: Gtk.Button) -> None:
        def picked(app: Gio.AppInfo) -> None:
            desktop_id = app.get_id() or ""
            base = desktop_id.removesuffix(".desktop")
            executable = os.path.basename(app.get_executable() or "")
            # AT-SPI reports the program name, which is usually the desktop ID
            # or the executable; launch requests use the full desktop ID.
            self._add_patterns([base, desktop_id, executable])

        AppPickerDialog(picked).present(self)

    # ------------------------------------------------------------- daemon

    def refresh_status(self) -> None:
        self._last_status_refresh = time.monotonic()
        self.run_async(
            lambda: BridgeClient(CLIENT_NAME, retries=1).status(),
            on_success=self._show_daemon,
            on_error=lambda _exc: self._show_daemon(None),
        )

    def _show_daemon(self, state: dict[str, Any] | None) -> None:
        self.daemon = state
        self.daemon_spinner.set_visible(False)
        if state is None:
            self.daemon_row.set_subtitle("Not running")
            self._show_banner("The bridge daemon is not running", "Start")
        else:
            error = state.get("settingsError")
            subtitle = f"Running · version {state.get('version')}"
            if error:
                subtitle += f" · settings error, holding Off: {error}"
            self.daemon_row.set_subtitle(GLib.markup_escape_text(subtitle))
            if state.get("accessMode") == "all":
                self._show_banner("All desktop access is enabled", "Stop all")
            else:
                self.banner.set_revealed(False)
        remote = (state or {}).get("remoteDesktop") or {}
        allowed = self.settings.access_mode == "all" and self.settings.allow_portal_input
        if remote.get("active"):
            devices = [name for name, active in (remote.get("devices") or {}).items() if active]
            self.remote_row.set_subtitle(f"Active · {' and '.join(devices) or 'no devices'}")
            self.remote_button.set_label("End")
            self.remote_button.remove_css_class("suggested-action")
            self.remote_button.set_sensitive(True)
        else:
            starting = remote.get("starting")
            self.remote_row.set_subtitle(
                "Waiting for your consent in the GNOME dialog…"
                if starting
                else "Inactive"
                if allowed
                else "Requires All desktop with global pointer and keyboard"
            )
            self.remote_button.set_label("Start")
            self.remote_button.add_css_class("suggested-action")
            self.remote_button.set_sensitive(bool(state) and allowed and not starting)
        self._update_agent()
        self._update_hero()

    def _update_agent(self) -> None:
        agent = (self.daemon or {}).get("agent")
        self.agent_live.set_visible(bool(agent))
        if agent:
            self.agent_row.set_title(GLib.markup_escape_text(agent.get("name", "Agent")))
            self.agent_avatar.set_text(agent.get("name", "Agent"))
            commands = agent.get("commands", 0)
            self.agent_row.set_subtitle(
                f"Connected {_since(agent.get('since'))} · "
                f"{commands} command{'s' if commands != 1 else ''}"
            )
        else:
            self.agent_row.set_title("No agent connected")
            self.agent_row.set_subtitle("Agents appear here as soon as they send a command")
            self.agent_avatar.set_text("")

    def _update_hero(self) -> None:
        mode = self.settings.access_mode
        agent = (self.daemon or {}).get("agent")
        for name in ("mode-observe", "mode-control", "mode-all", "offline", "agent"):
            self.badge.remove_css_class(name)
        if self.daemon is None:
            self.badge.add_css_class("offline")
        elif mode != "off":
            self.badge.add_css_class(f"mode-{mode}")
        if agent:
            self.badge.add_css_class("agent")
        self.badge_icon.set_from_icon_name(
            "network-offline-symbolic" if self.daemon is None else MODE_ICONS[mode]
        )
        if self.daemon is None:
            self.hero_title.set_label("Bridge offline")
        elif agent:
            self.hero_title.set_label(f"{agent.get('name', 'Agent')} is connected")
        else:
            self.hero_title.set_label(MODE_TITLES[mode])
        remote = ((self.daemon or {}).get("remoteDesktop") or {}).get("active")
        subtitle = MODE_DESCRIPTIONS[mode]
        if agent:
            subtitle = f"Mode: {MODE_TITLES[mode]}"
            if remote:
                subtitle += " · controlling pointer and keyboard"
        self.hero_subtitle.set_label(subtitle)

    def _show_banner(self, title: str, button: str) -> None:
        self.banner.set_title(title)
        self.banner.set_button_label(button)
        self.banner.set_revealed(True)

    def _on_banner_button(self, _banner: Adw.Banner) -> None:
        if self.daemon is None:
            self._start_daemon()
        else:
            self.stop_all()

    def _start_daemon(self) -> None:
        self.daemon_spinner.set_visible(True)

        def work() -> None:
            process = Gio.Subprocess.new(
                ["systemctl", "--user", "start", SERVICE_NAME],
                Gio.SubprocessFlags.STDERR_PIPE,
            )
            ok, _stdout, stderr = process.communicate_utf8(None, None)
            if not process.get_successful():
                raise RuntimeError((stderr or "").strip() or "systemctl failed")

        self.run_async(
            work,
            on_success=lambda _result: GLib.timeout_add(600, self._refresh_once),
            on_error=lambda exc: (
                self.daemon_spinner.set_visible(False),
                self.toast(f"Could not start the daemon: {exc}"),
            ),
        )

    def _refresh_once(self) -> bool:
        self.refresh_status()
        return GLib.SOURCE_REMOVE

    def _on_remote_button(self, button: Gtk.Button) -> None:
        active = ((self.daemon or {}).get("remoteDesktop") or {}).get("active")
        action = "stop_remote_desktop" if active else "start_remote_desktop"
        button.set_sensitive(False)
        if not active:
            self.remote_row.set_subtitle("Waiting for your consent in the GNOME dialog…")
        self.run_async(
            lambda: BridgeClient(CLIENT_NAME).command(action),
            on_success=lambda _result: self.refresh_status(),
            on_error=lambda exc: (self.toast(str(exc)), self.refresh_status()),
        )

    def stop_all(self) -> None:
        """Revoke on disk first so the stop works even if the daemon hangs."""

        self.settings, error = self.store.force_off()
        self._sync_settings()
        self.run_async(
            lambda: BridgeClient("cli-admin", retries=2).command("stop_all"),
            on_success=lambda _result: (
                self.toast("All agent access stopped"),
                self.refresh_status(),
            ),
            on_error=lambda _exc: self.toast(
                "Switched to Off"
                + (f", but saving failed: {error}" if error else "; the daemon is not running")
            ),
        )

    # ------------------------------------------------------------ activity

    def _stream_loop(self) -> None:
        while not self._closing.is_set():
            try:
                client = BridgeClient(CLIENT_NAME, retries=1)
                for event in client.stream_events(after=self._latest_event):
                    if self._closing.is_set():
                        return
                    self._latest_event = max(self._latest_event, int(event.get("id", 0)))
                    GLib.idle_add(self._on_event, event)
            except (ClientError, OSError, ValueError):
                pass
            self._closing.wait(STREAM_RETRY_SECONDS)

    def _on_event(self, event: dict[str, Any]) -> bool:
        kind = str(event.get("type", ""))
        if kind.startswith(("agent.", "settings.", "portal.", "security.", "daemon.")) or (
            kind in {"command.completed", "command.failed"} and self.daemon is None
        ):
            self.refresh_status()
        elif kind == "command.completed" and (self.daemon or {}).get("agent"):
            agent = self.daemon["agent"]
            agent["commands"] = agent.get("commands", 0) + 1
            self._update_agent()
        presentation = _event_presentation(event)
        if presentation is None:
            return GLib.SOURCE_REMOVE
        data = event.get("data") or {}
        if data.get("client") == CLIENT_NAME and kind.startswith("command."):
            return GLib.SOURCE_REMOVE
        icon, style, title, subtitle = presentation
        stamp = str(event.get("timestamp", ""))
        try:
            local = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
            stamp = local.strftime("%H:%M:%S")
        except ValueError:
            stamp = ""
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(title),
            subtitle=GLib.markup_escape_text(subtitle),
        )
        image = Gtk.Image(icon_name=icon, valign=Gtk.Align.CENTER)
        image.add_css_class("bridge-event-icon")
        if style:
            image.add_css_class(style)
        row.add_prefix(image)
        time_label = Gtk.Label(label=stamp, valign=Gtk.Align.CENTER)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("numeric")
        time_label.add_css_class("caption")
        row.add_suffix(time_label)
        revealer = Gtk.Revealer(
            child=row,
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=220,
        )
        self.activity_list.prepend(revealer)
        GLib.idle_add(lambda: (revealer.set_reveal_child(True), GLib.SOURCE_REMOVE)[1])
        self._activity_count += 1
        if self._activity_count > MAX_ACTIVITY_ROWS:
            last = self.activity_list.get_row_at_index(MAX_ACTIVITY_ROWS)
            if last is not None:
                self.activity_list.remove(last)
                self._activity_count -= 1
        self.activity_stack.set_visible_child_name("list")
        return GLib.SOURCE_REMOVE

    def _clear_activity(self) -> None:
        self.activity_list.remove_all()
        self._activity_count = 0
        self.activity_stack.set_visible_child_name("empty")

    # ----------------------------------------------------------- extension

    def _check_extension(self) -> None:
        def work() -> dict[str, Any]:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync(
                "org.gnome.Shell.Extensions",
                "/org/gnome/Shell/Extensions",
                "org.gnome.Shell.Extensions",
                "GetExtensionInfo",
                GLib.Variant("(s)", (EXTENSION_UUID,)),
                GLib.VariantType.new("(a{sv})"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return reply.unpack()[0]

        self.run_async(work, on_success=self._show_extension, on_error=self._extension_error)

    def _show_extension(self, info: dict[str, Any]) -> None:
        state = int(info.get("state", 0)) if info else 0
        self.extension_button.set_visible(False)
        if not info:
            self.extension_row.set_subtitle(
                "Not installed. Run scripts/install-user.sh, then log out and back in."
            )
        elif state == 1:
            clients = ((self.daemon or {}).get("overlay") or {}).get("clients", 0)
            self.extension_row.set_subtitle(
                "Enabled · connected to the bridge" if clients else "Enabled"
            )
        elif state == 3:
            self.extension_row.set_subtitle(
                GLib.markup_escape_text(f"Error: {info.get('error', 'unknown')}")
            )
        elif state == 4:
            self.extension_row.set_subtitle("Not compatible with this GNOME Shell version")
        else:
            self.extension_row.set_subtitle("Installed but turned off")
            self.extension_button.set_visible(True)

    def _extension_error(self, _exc: Exception) -> None:
        self.extension_row.set_subtitle("GNOME Shell is not reachable")

    def _on_enable_extension(self, _button: Gtk.Button) -> None:
        def work() -> None:
            Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
                "org.gnome.Shell.Extensions",
                "/org/gnome/Shell/Extensions",
                "org.gnome.Shell.Extensions",
                "EnableExtension",
                GLib.Variant("(s)", (EXTENSION_UUID,)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )

        self.run_async(work, on_success=lambda _result: self._check_extension())

    # --------------------------------------------------------------- token

    def _show_token(self) -> None:
        token = load_or_create_token(self.paths)
        self.token_row.set_subtitle(f"{token[:6]}••••••••{token[-4:]}")

    def _on_copy_token(self, _button: Gtk.Button) -> None:
        clipboard = self.get_clipboard()
        clipboard.set(load_or_create_token(self.paths))
        self.toast("Token copied")

    def _on_rotate_token(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog(
            heading="Rotate the token?",
            body="Every client using the current token is disconnected immediately.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rotate", "Rotate")
        dialog.set_response_appearance("rotate", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def chosen(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            if source.choose_finish(result) != "rotate":
                return
            self.paths.ensure()
            _atomic_write(self.paths.token_file, secrets.token_hex(32) + "\n")
            self._show_token()
            self.toast("Token rotated")

        dialog.choose(self, None, chosen)

    # ---------------------------------------------------------------- utils

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def run_async(
        self,
        work: Callable[[], Any],
        *,
        on_success: Callable[[Any], Any] | None = None,
        on_error: Callable[[Exception], Any] | None = None,
    ) -> None:
        def finish(callback: Callable[..., Any] | None, value: Any) -> bool:
            if callback is not None and not self._closing.is_set():
                callback(value)
            return GLib.SOURCE_REMOVE

        def runner() -> None:
            try:
                value = work()
            except Exception as exc:
                if on_error is None:
                    GLib.idle_add(finish, self.toast, str(exc))
                else:
                    GLib.idle_add(finish, on_error, exc)
                return
            GLib.idle_add(finish, on_success, value)

        threading.Thread(target=runner, daemon=True, name="bridge-control-task").start()

    def _on_close(self, _window: Gtk.Window) -> bool:
        self._closing.set()
        return False


class ControlCenter(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.paths = AppPaths.discover()
        self.store = SettingsStore(self.paths)
        self.window: BridgeWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        stop = Gio.SimpleAction.new("stop-all", None)
        stop.connect("activate", lambda *_args: self.window and self.window.stop_all())
        self.add_action(stop)
        self.set_accels_for_action("app.stop-all", ["<Control><Shift>Escape"])
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self) -> None:
        if self.window is None:
            self.window = BridgeWindow(self)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    app = ControlCenter()
    return app.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
