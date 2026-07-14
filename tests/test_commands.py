from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gnome_desktop_bridge.commands import CommandDispatcher, _sanitize_for_audit
from gnome_desktop_bridge.config import SettingsStore
from gnome_desktop_bridge.errors import AccessDenied, InvalidRequest
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


class FakePortal:
    def __init__(self) -> None:
        self.active = False
        self.stopped = 0

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
        return {"performed": True, **kwargs}

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
        self.dispatcher = CommandDispatcher(
            self.store,
            self.events,
            atspi=self.atspi,  # type: ignore[arg-type]
            portal=self.portal,  # type: ignore[arg-type]
            apps=FakeApps(),  # type: ignore[arg-type]
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


if __name__ == "__main__":
    unittest.main()
