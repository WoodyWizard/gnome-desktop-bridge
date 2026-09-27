from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gnome_desktop_bridge import commands
from gnome_desktop_bridge.commands import CommandDispatcher, _sanitize_for_audit
from gnome_desktop_bridge.config import SettingsStore
from gnome_desktop_bridge.errors import AccessDenied, BridgeError, InvalidRequest
from gnome_desktop_bridge.events import EventBuffer
from gnome_desktop_bridge.policy import AppIdentity

from .test_config import temporary_paths


class FakeAtspi:
    identity = AppIdentity(name="Test Editor", app_id="com.example.Editor", pid=10)

    def available(self) -> tuple[bool, None]:
        return True, None

    def identity_for_ref(self, ref: str) -> AppIdentity:
        return self.identity

    def fill(self, ref: str, text: str) -> dict[str, Any]:
        return {"ref": ref, "performed": True, "textLength": len(text)}

    def invoke(self, ref: str, *, action: int | str = 0) -> dict[str, Any]:
        return {"ref": ref, "performed": True, "actionIndex": action}

    def focus(self, ref: str) -> dict[str, Any]:
        return {"ref": ref, "performed": True}

    def bounds_for_ref(self, ref: str) -> tuple[AppIdentity, dict[str, int]]:
        return self.identity, {"x": 10, "y": 20, "width": 100, "height": 40}

    def describe_ref(self, ref: str) -> dict[str, Any]:
        return {
            "ref": ref,
            "app": self.identity,
            "role": "push button",
            "name": "Save",
            "bounds": {"x": 10, "y": 20, "width": 100, "height": 40},
            "window": {"name": "Editor", "bounds": {"x": 0, "y": 0, "width": 800, "height": 600}},
        }


class FakeShell:
    def __init__(self, origin: tuple[bool, int, int] | None = None) -> None:
        self.origin = origin
        self.calls: list[tuple[Any, ...]] = []

    def window_origin(self, *args: Any) -> tuple[bool, int, int] | None:
        self.calls.append(args)
        return self.origin


class FakePortal:
    def __init__(self) -> None:
        self.active = False
        self.stopped = 0
        self.clicks: list[dict[str, Any]] = []

    def available(self) -> tuple[bool, None]:
        return True, None

    def versions(self) -> dict[str, int]:
        return {"remoteDesktop": 2}

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "devices": {}, "streams": []}

    def start_remote_desktop(self, *, persist: bool = False) -> dict[str, Any]:
        self.active = True
        return self.status()

    def stop_remote_desktop(self) -> dict[str, Any]:
        self.active = False
        self.stopped += 1
        return self.status()

    def pointer_click(self, **kwargs: Any) -> dict[str, Any]:
        self.clicks.append(kwargs)
        return {"performed": True, **kwargs}

    def pointer_move(self, x: float, y: float, *, duration_ms: int = 0) -> dict[str, Any]:
        return {"performed": True, "x": x, "y": y, "durationMs": duration_ms}

    def key(self, key: Any, *, event: str = "tap", modifiers: Any = None) -> dict[str, Any]:
        return {"performed": True, "key": key, "event": event}

    def type_text(self, text: str, *, interval_ms: int) -> dict[str, Any]:
        return {"performed": True, "textLength": len(text)}

    def shutdown(self) -> None:
        pass


class FakeApps:
    pass


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = temporary_paths(Path(self.temporary.name))
        self.store = SettingsStore(self.paths)
        self.events = EventBuffer()
        self.atspi = FakeAtspi()
        self.portal = FakePortal()
        self.shell = FakeShell()
        self.dispatcher = CommandDispatcher(
            self.store,
            self.events,
            atspi=self.atspi,  # type: ignore[arg-type]
            portal=self.portal,  # type: ignore[arg-type]
            apps=FakeApps(),  # type: ignore[arg-type]
            shell=self.shell,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fill_is_scoped_and_plaintext_never_enters_audit_log(self) -> None:
        self.store.update(access_mode="control", allowed_apps=["com.example.*"])
        secret = "very secret text"
        result = self.dispatcher.dispatch(
            {"action": "fill", "args": {"ref": "g1:n2", "text": secret}}
        )
        self.assertTrue(result["performed"])
        rendered = json.dumps(self.events.after(0))
        self.assertNotIn(secret, rendered)
        self.assertIn("textRedacted", rendered)

    def test_fill_is_denied_outside_allowlist(self) -> None:
        self.store.update(access_mode="control", allowed_apps=["org.mozilla.*"])
        with self.assertRaises(AccessDenied):
            self.dispatcher.dispatch({"action": "fill", "args": {"ref": "g1:n2", "text": "hello"}})

    def test_global_text_is_redacted_from_audit(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        secret = "not in logs"
        self.dispatcher.dispatch({"action": "type_text", "args": {"text": secret, "intervalMs": 0}})
        self.assertNotIn(secret, json.dumps(self.events.after(0)))

    def test_nested_sensitive_audit_fields_are_redacted_and_bounded(self) -> None:
        secret = "nested password value"
        sanitized = _sanitize_for_audit(
            {
                "text": "safe to redact",
                "metadata": {"password": secret, "note": "x" * 2000},
            }
        )
        rendered = json.dumps(sanitized)
        self.assertNotIn(secret, rendered)
        self.assertLess(len(rendered), 10_000)

    def test_coordinate_click_requires_all_desktop(self) -> None:
        self.store.update(access_mode="control", allowed_apps=["com.example.*"])
        with self.assertRaises(AccessDenied):
            self.dispatcher.dispatch(
                {"action": "click", "args": {"ref": "g1:n2", "method": "coordinates"}}
            )

    def test_unknown_argument_is_rejected_instead_of_silently_misclicking(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        with self.assertRaises(InvalidRequest):
            self.dispatcher.dispatch({"action": "pointer_click", "args": {"X": 10, "y": 20}})

    def test_stop_all_closes_portal_and_persists_off(self) -> None:
        self.portal.active = True
        self.store.update(access_mode="all", allow_portal_input=True)
        result = self.dispatcher.dispatch({"action": "stop_all", "args": {}})
        self.assertTrue(result["stopped"])
        self.assertFalse(self.portal.active)
        settings = self.store.get()
        self.assertEqual(settings.access_mode, "off")
        self.assertFalse(settings.allow_portal_input)
        self.assertTrue(result["settingsPersisted"])

    def test_stop_all_stays_fail_closed_when_settings_cannot_be_written(self) -> None:
        self.portal.active = True
        self.store.update(access_mode="all", allow_portal_input=True)
        with patch(
            "gnome_desktop_bridge.config.save_settings",
            side_effect=OSError("read-only filesystem"),
        ):
            result = self.dispatcher.dispatch({"action": "stop_all", "args": {}})
        self.assertFalse(result["settingsPersisted"])
        self.assertEqual(self.store.get().access_mode, "off")
        self.assertFalse(self.portal.active)

    def test_policy_watcher_revokes_stale_portal_session(self) -> None:
        self.portal.active = True
        self.store.update(access_mode="observe")
        self.dispatcher.enforce_current_policy()
        self.assertFalse(self.portal.active)
        self.assertEqual(self.portal.stopped, 1)

    def test_revocation_is_announced_once_while_a_session_winds_down(self) -> None:
        self.store.update(access_mode="observe")
        with patch.object(FakePortal, "stop_remote_desktop", return_value={}):
            self.portal.active = True
            for _ in range(3):
                self.dispatcher.enforce_current_policy()
        self.assertEqual(self.event_types().count("portal.session.revoked"), 1)


    def event_types(self) -> list[str]:
        return [event["type"] for event in self.events.after(0, limit=1000)]

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        return [event["data"] for event in self.events.after(0, limit=1000) if event["type"] == kind]

    def test_first_agent_command_announces_presence_once(self) -> None:
        self.dispatcher.dispatch({"action": "ping", "args": {}}, client="api")
        self.dispatcher.dispatch({"action": "capabilities", "args": {}}, client="api")
        self.assertEqual(self.event_types().count("agent.connected"), 1)
        self.assertEqual(self.dispatcher.agent()["name"], "AI agent")
        self.assertEqual(self.dispatcher.agent()["commands"], 2)

    def test_human_clients_never_look_like_an_agent(self) -> None:
        for client in ("control-center", "overlay", "cli-admin"):
            self.dispatcher.dispatch({"action": "get_state", "args": {}}, client=client)
        self.assertNotIn("agent.connected", self.event_types())
        self.assertIsNone(self.dispatcher.agent())

    def test_hello_names_the_agent_and_goodbye_disconnects(self) -> None:
        self.dispatcher.dispatch({"action": "hello", "args": {"name": " Claude  "}}, client="api")
        self.assertEqual(self.events_of("agent.connected")[0]["name"], "Claude")
        result = self.dispatcher.dispatch({"action": "goodbye", "args": {}}, client="api")
        self.assertTrue(result["disconnected"])
        self.assertEqual(self.events_of("agent.disconnected")[0]["reason"], "goodbye")
        with self.assertRaises(InvalidRequest):
            self.dispatcher.dispatch({"action": "hello", "args": {"name": "x" * 65}})

    def test_idle_agent_is_disconnected_by_the_watcher(self) -> None:
        self.dispatcher.dispatch({"action": "ping", "args": {}}, client="api")
        with patch.object(commands, "AGENT_IDLE_TIMEOUT", -1):
            self.dispatcher.tick()
        self.assertEqual(self.events_of("agent.disconnected")[0]["reason"], "idle")
        self.assertIsNone(self.dispatcher.agent())

    def test_stop_all_disconnects_the_agent(self) -> None:
        self.dispatcher.dispatch({"action": "ping", "args": {}}, client="Claude")
        self.dispatcher.dispatch({"action": "stop_all", "args": {}}, client="Claude")
        self.assertEqual(self.events_of("agent.disconnected")[0]["reason"], "stop_all")

    def test_watcher_announces_settings_changes(self) -> None:
        self.dispatcher.tick()
        self.store.update(access_mode="observe")
        self.dispatcher.tick()
        self.assertEqual(self.events_of("settings.changed")[0]["accessMode"], "observe")

    def test_visual_cues_only_flow_to_an_attached_overlay(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        click = {"action": "pointer_click", "args": {"x": 5, "y": 6, "durationMs": 0}}
        self.dispatcher.dispatch(click)
        self.assertFalse([kind for kind in self.event_types() if kind.startswith("visual.")])
        with self.events.subscribed("overlay"):
            self.dispatcher.dispatch(click)
        self.assertEqual(self.events_of("visual.pointer")[0]["x"], 5.0)
        self.assertEqual(self.events_of("visual.click")[0]["count"], 1)
        self.store.update(overlay_enabled=False)
        with self.events.subscribed("overlay"):
            self.dispatcher.dispatch(click)
        self.assertEqual(len(self.events_of("visual.click")), 1)

    def test_overlay_never_sees_typed_characters(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        with self.events.subscribed("overlay"):
            self.dispatcher.dispatch({"action": "key", "args": {"key": "p"}})
            self.dispatcher.dispatch(
                {"action": "key", "args": {"key": "l", "modifiers": ["Control_L"]}}
            )
            self.dispatcher.dispatch({"action": "key", "args": {"key": "Return"}})
            self.dispatcher.dispatch({"action": "type_text", "args": {"text": "secret"}})
        keys = [data["keys"] for data in self.events_of("visual.key")]
        self.assertEqual(keys, [["•"], ["Control_L", "l"], ["Return"]])
        self.assertNotIn("secret", json.dumps(self.events.after(0, limit=1000)))
        self.assertEqual(self.events_of("visual.type")[0]["length"], 6)

    def test_semantic_action_highlights_target_in_screen_coordinates(self) -> None:
        self.store.update(access_mode="control", allowed_apps=["com.example.*"])
        self.shell.origin = (True, 300, 200)
        with self.events.subscribed("overlay"):
            self.dispatcher.dispatch({"action": "invoke", "args": {"ref": "g1:n2"}})
        target = self.events_of("visual.target")[0]
        self.assertEqual(target["bounds"], {"x": 310, "y": 220, "width": 100, "height": 40})
        self.assertEqual(target["name"], "Save")

    def test_coordinate_click_maps_window_relative_bounds(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        self.shell.origin = (True, 300, 200)
        result = self.dispatcher.dispatch(
            {"action": "click", "args": {"ref": "g1:n2", "method": "coordinates"}}
        )
        self.assertEqual((result["x"], result["y"]), (360.0, 240.0))

    def test_coordinate_click_fails_closed_without_window_geometry_on_wayland(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        self.shell.origin = None
        with (
            patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}),
            self.assertRaises(BridgeError) as raised,
        ):
            self.dispatcher.dispatch(
                {"action": "click", "args": {"ref": "g1:n2", "method": "coordinates"}}
            )
        self.assertEqual(raised.exception.code, "coordinates_unavailable")
        self.assertEqual(self.portal.clicks, [])

    def test_malformed_argument_types_are_rejected_not_crashing(self) -> None:
        self.store.update(access_mode="all", allow_portal_input=True)
        bad_requests = [
            {"action": "click", "args": {"ref": "g1:n2", "method": ["coordinates"]}},
            {"action": "pointer_move", "args": {"x": True, "y": 1}},
            {"action": "pointer_move", "args": {"x": float("nan"), "y": 1}},
            {"action": "pointer_click", "args": {"x": 1}},
            {"action": "scroll", "args": {"dy": "down"}},
        ]
        for request in bad_requests:
            with self.subTest(request=request), self.assertRaises(InvalidRequest):
                self.dispatcher.dispatch(request)


if __name__ == "__main__":
    unittest.main()
