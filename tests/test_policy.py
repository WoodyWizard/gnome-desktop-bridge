from __future__ import annotations

import unittest

from gnome_desktop_bridge.config import Settings
from gnome_desktop_bridge.errors import AccessDenied, InvalidRequest
from gnome_desktop_bridge.policy import AppIdentity, Policy, app_matches


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy()
        self.firefox = AppIdentity(name="Firefox", app_id="org.mozilla.firefox", pid=123)

    def test_off_allows_only_introspection_and_emergency_stop(self) -> None:
        settings = Settings()
        for action in ("ping", "capabilities", "get_state", "stop_all"):
            self.policy.authorize(action, settings)
        with self.assertRaises(AccessDenied):
            self.policy.authorize("snapshot", settings)

    def test_observe_is_read_only(self) -> None:
        settings = Settings(access_mode="observe")
        self.policy.authorize("list_apps", settings)
        self.policy.authorize("snapshot", settings)
        with self.assertRaises(AccessDenied):
            self.policy.authorize("invoke", settings, app=self.firefox)

    def test_control_requires_matching_application(self) -> None:
        settings = Settings(access_mode="control", allowed_apps=["org.mozilla.*"])
        self.policy.authorize("invoke", settings, app=self.firefox)
        with self.assertRaises(AccessDenied):
            self.policy.authorize(
                "invoke",
                settings,
                app=AppIdentity(name="Passwords", app_id="org.gnome.Seahorse"),
            )

    def test_all_requires_separate_global_input_gate(self) -> None:
        settings = Settings(access_mode="all", allow_portal_input=False)
        self.policy.authorize("invoke", settings, app=self.firefox)
        with self.assertRaises(AccessDenied):
            self.policy.authorize("pointer_click", settings)
        settings.allow_portal_input = True
        self.policy.authorize("pointer_click", settings)

    def test_screenshot_and_launch_feature_gates(self) -> None:
        settings = Settings(access_mode="observe", allow_screenshots=False)
        with self.assertRaises(AccessDenied):
            self.policy.authorize("screenshot", settings)
        settings = Settings(
            access_mode="control",
            allowed_apps=["*"],
            allow_launch_apps=False,
        )
        with self.assertRaises(AccessDenied):
            self.policy.authorize("launch_app", settings, app=self.firefox)

    def test_globs_are_case_insensitive(self) -> None:
        self.assertTrue(app_matches(self.firefox, ["ORG.MOZILLA.*"]))
        self.assertTrue(app_matches(self.firefox, ["fire*"]))
        self.assertFalse(app_matches(self.firefox, ["chromium*"]))

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.policy.authorize("destroy_everything", Settings())


if __name__ == "__main__":
    unittest.main()
