from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from gnome_desktop_bridge.errors import BackendUnavailable, InvalidRequest
from gnome_desktop_bridge.portal_backend import (
    DEVICE_KEYBOARD,
    DEVICE_POINTER,
    KEY_PRESSED,
    KEY_RELEASED,
    PortalBackend,
    PortalStream,
)

from .test_config import temporary_paths


class PortalPureTests(unittest.TestCase):
    def test_stream_metadata_is_parsed_for_coordinate_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = PortalBackend(temporary_paths(Path(directory)))
            streams = backend._parse_streams(  # noqa: SLF001 - pure parser contract
                [
                    (42, {"position": (1920, 0), "size": (2560, 1440), "mapping_id": "DP-1"}),
                    (43, {}),
                ]
            )
            self.assertEqual(streams[0].node_id, 42)
            self.assertTrue(streams[0].contains(2000, 100))
            self.assertFalse(streams[0].contains(100, 100))
            self.assertTrue(streams[1].contains(100, 100))



class RecordingPortal(PortalBackend):
    """A portal with a fake active session that records D-Bus notifications."""

    def __init__(self, paths: Any) -> None:
        super().__init__(paths)
        self._session_handle = "/session"
        self._devices = DEVICE_KEYBOARD | DEVICE_POINTER
        self._streams = [PortalStream(node_id=7, x=0, y=0, width=1000, height=800)]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_on: int | None = None

    def _notify(self, method: str, signature: str, values: tuple[Any, ...]) -> None:
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            self.calls.append(("FAILED", values))
            raise BackendUnavailable("portal", "simulated failure")
        self.calls.append((method, values))

    def _keyval(self, key: str | int) -> int:
        names = {"Return": 0xFF0D, "Control_L": 0xFFE3, "Shift_L": 0xFFE1}
        key = {"\n": "Return"}.get(key, key) if isinstance(key, str) else key
        return names.get(key, ord(key[0]) if isinstance(key, str) else key)


class PortalInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.portal = RecordingPortal(temporary_paths(Path(self.temporary.name)))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def motions(self) -> list[tuple[float, float]]:
        return [
            (values[3], values[4])
            for method, values in self.portal.calls
            if method == "NotifyPointerMotionAbsolute"
        ]

    def test_animated_motion_follows_an_eased_path_to_the_exact_target(self) -> None:
        self.portal.pointer_move(100, 100)
        self.portal.pointer_move(500, 300, duration_ms=60)
        path = self.motions()[1:]
        self.assertGreater(len(path), 2)
        self.assertEqual(path[-1], (500.0, 300.0))
        xs = [x for x, _y in path]
        self.assertEqual(xs, sorted(xs))

    def test_first_move_jumps_because_the_start_is_unknown(self) -> None:
        result = self.portal.pointer_move(10, 10, duration_ms=500)
        self.assertEqual(len(self.motions()), 1)
        self.assertEqual(result["durationMs"], 0)

    def test_non_finite_and_boolean_coordinates_are_rejected(self) -> None:
        for x in (float("nan"), float("inf"), True):
            with self.subTest(x=x), self.assertRaises(InvalidRequest):
                self.portal.pointer_move(x, 1)

    def test_modifiers_are_released_even_when_the_key_fails(self) -> None:
        self.portal.fail_on = 1  # the main key press fails after Control_L went down
        with self.assertRaises(BackendUnavailable):
            self.portal.key("l", modifiers=["Control_L"])
        last_method, last_values = self.portal.calls[-1]
        self.assertEqual(last_method, "NotifyKeyboardKeysym")
        self.assertEqual(last_values[2:], (0xFFE3, KEY_RELEASED))

    def test_newlines_are_typed_as_return(self) -> None:
        self.portal.type_text("a\r\nb", interval_ms=0)
        pressed = [
            values[2]
            for method, values in self.portal.calls
            if method == "NotifyKeyboardKeysym" and values[3] == KEY_PRESSED
        ]
        self.assertEqual(pressed, [ord("a"), 0xFF0D, ord("b")])

    def test_double_click_sends_two_press_release_pairs(self) -> None:
        self.portal.pointer_click(count=2)
        buttons = [values[3] for method, values in self.portal.calls if method == "NotifyPointerButton"]
        self.assertEqual(buttons, [KEY_PRESSED, KEY_RELEASED, KEY_PRESSED, KEY_RELEASED])


if __name__ == "__main__":
    unittest.main()
