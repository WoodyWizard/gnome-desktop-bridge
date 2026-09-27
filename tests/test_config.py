from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from gnome_desktop_bridge.config import (
    AppPaths,
    Settings,
    SettingsStore,
    TokenStore,
    _atomic_write,
    load_or_create_token,
    load_settings,
    save_settings,
)
from gnome_desktop_bridge.errors import InvalidRequest


def temporary_paths(root: Path) -> AppPaths:
    return AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        runtime_dir=root / "runtime",
        settings_file=root / "config" / "settings.json",
        token_file=root / "data" / "token",
        screenshots_dir=root / "data" / "screenshots",
    )


class SettingsTests(unittest.TestCase):
    def test_default_settings_are_fail_closed(self) -> None:
        settings = Settings()
        self.assertEqual(settings.access_mode, "off")
        self.assertFalse(settings.allow_portal_input)
        self.assertEqual(settings.allowed_apps, [])

    def test_round_trip_uses_public_json_names_and_secure_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            expected = Settings(
                access_mode="control",
                allowed_apps=["Firefox*", "org.gnome.Terminal"],
                allow_launch_apps=True,
            )
            save_settings(expected, paths)
            actual = load_settings(paths)
            self.assertEqual(actual.to_json_dict(), expected.to_json_dict())
            raw = json.loads(paths.settings_file.read_text())
            self.assertIn("accessMode", raw)
            self.assertNotIn("access_mode", raw)
            self.assertEqual(stat.S_IMODE(paths.settings_file.stat().st_mode), 0o600)

    def test_unknown_keys_and_non_loopback_host_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            Settings.from_json_dict({"version": 1, "mystery": True})
        with self.assertRaises(InvalidRequest):
            Settings(host="0.0.0.0")

    def test_store_notices_external_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            store = SettingsStore(paths)
            changed = store.get()
            changed.access_mode = "observe"
            save_settings(changed, paths)
            self.assertEqual(store.get().access_mode, "observe")

    def test_invalid_external_settings_fail_closed_without_destroying_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            store = SettingsStore(paths)
            store.update(access_mode="all", allow_portal_input=True)
            paths.settings_file.write_text('{"accessMode": "broken"}')
            self.assertEqual(store.get().access_mode, "off")
            self.assertFalse(store.get().allow_portal_input)
            self.assertIsNotNone(store.last_error)
            self.assertIn("broken", paths.settings_file.read_text())

    def test_token_is_stable_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            first = load_or_create_token(paths)
            second = load_or_create_token(paths)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            bytes.fromhex(first)
            self.assertEqual(stat.S_IMODE(paths.token_file.stat().st_mode), 0o600)

    def test_token_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            paths.ensure()
            target = Path(directory) / "outside-token"
            target.write_text("a" * 64)
            paths.token_file.symlink_to(target)
            with self.assertRaises(InvalidRequest):
                load_or_create_token(paths)


    def test_overlay_settings_round_trip_and_old_files_get_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            paths.ensure()
            # A file written before the overlay existed must still load.
            paths.settings_file.write_text('{"version": 1, "accessMode": "observe"}')
            paths.settings_file.chmod(0o600)
            loaded = load_settings(paths)
            self.assertTrue(loaded.overlay_enabled)
            self.assertEqual(loaded.pointer_motion_ms, 200)
            save_settings(Settings(overlay_show_keys=False, pointer_motion_ms=0), paths)
            raw = json.loads(paths.settings_file.read_text())
            self.assertFalse(raw["overlayShowKeys"])
            self.assertEqual(raw["pointerMotionMs"], 0)

    def test_field_types_are_checked_for_programmatic_updates_too(self) -> None:
        with self.assertRaises(InvalidRequest):
            Settings(pointer_motion_ms=True)  # type: ignore[arg-type]
        with self.assertRaises(InvalidRequest):
            Settings(overlay_enabled="yes")  # type: ignore[arg-type]
        with self.assertRaises(InvalidRequest):
            Settings(pointer_motion_ms=5000)


class TokenStoreTests(unittest.TestCase):
    def test_rotation_is_picked_up_and_broken_replacement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = temporary_paths(Path(directory))
            store = TokenStore(paths)
            first = store.get()
            self.assertEqual(first, load_or_create_token(paths))
            _atomic_write(paths.token_file, "b" * 64 + "\n")
            self.assertEqual(store.get(), "b" * 64)
            _atomic_write(paths.token_file, "not a token\n")
            self.assertIsNone(store.get())


if __name__ == "__main__":
    unittest.main()
