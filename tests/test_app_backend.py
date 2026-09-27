from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from gnome_desktop_bridge.app_backend import AppBackend
from gnome_desktop_bridge.errors import BackendUnavailable, NotFound
from gnome_desktop_bridge.policy import AppIdentity


class AppLaunchTests(unittest.TestCase):
    def test_hardened_service_launches_through_fresh_systemd_unit(self) -> None:
        backend = AppBackend()
        identity = AppIdentity(name="Example", app_id="org.example.App.desktop")
        completed = subprocess.CompletedProcess([], 0, "", "")

        def executable(name: str) -> str | None:
            return {"systemd-run": "/usr/bin/systemd-run", "gtk-launch": "/usr/bin/gtk-launch"}.get(
                name
            )

        with (
            patch("gnome_desktop_bridge.app_backend.shutil.which", side_effect=executable),
            patch(
                "gnome_desktop_bridge.app_backend.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            result = backend.launch_resolved(Mock(), identity)

        self.assertEqual(result["backend"], "systemd-run+gtk-launch")
        arguments = run.call_args.args[0]
        self.assertIn("--user", arguments)
        self.assertEqual(arguments[-2:], ["/usr/bin/gtk-launch", identity.app_id])

    def test_hardened_service_does_not_spawn_apps_inside_its_own_sandbox(self) -> None:
        backend = AppBackend()
        app = Mock()
        identity = AppIdentity(name="Example", app_id="org.example.App.desktop")
        with (
            patch("gnome_desktop_bridge.app_backend.shutil.which", return_value=None),
            patch.dict(os.environ, {"INVOCATION_ID": "test"}),
            self.assertRaises(BackendUnavailable),
        ):
            backend.launch_resolved(app, identity)
        app.launch.assert_not_called()


    def test_unknown_desktop_id_is_not_found_instead_of_crashing(self) -> None:
        backend = AppBackend()
        try:
            backend._ensure()  # noqa: SLF001
        except BackendUnavailable as exc:
            self.skipTest(str(exc))
        with self.assertRaises(NotFound):
            backend.resolve("org.example.DoesNotExist-7f3a")


if __name__ == "__main__":
    unittest.main()
