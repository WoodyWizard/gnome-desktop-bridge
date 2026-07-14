from __future__ import annotations

import threading
import time
import unittest

from gnome_desktop_bridge.events import EventBuffer


class EventBufferTests(unittest.TestCase):
    def test_buffer_is_bounded_and_ids_remain_monotonic(self) -> None:
        events = EventBuffer(2)
        events.emit("one")
        events.emit("two")
        events.emit("three")
        selected = events.after(0)
        self.assertEqual([event["id"] for event in selected], [2, 3])
        self.assertEqual(events.latest_id(), 3)

    def test_wait_after_wakes_for_new_event(self) -> None:
        events = EventBuffer()

        def emit() -> None:
            time.sleep(0.02)
            events.emit("ready", data={"value": 1})

        thread = threading.Thread(target=emit)
        thread.start()
        selected = events.wait_after(0, timeout=1)
        thread.join()
        self.assertEqual(selected[0]["type"], "ready")

    def test_event_data_is_copied(self) -> None:
        events = EventBuffer()
        data = {"nested": {"value": 1}}
        events.emit("copy", data=data)
        data["nested"]["value"] = 2
        self.assertEqual(events.after(0)[0]["data"]["nested"]["value"], 1)

    def test_concurrent_emitters_receive_unique_ordered_ids(self) -> None:
        events = EventBuffer(1000)
        threads = [
            threading.Thread(target=lambda: [events.emit("parallel") for _ in range(50)])
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = [event["id"] for event in events.after(0, limit=1000)]
        self.assertEqual(ids, list(range(1, 401)))


if __name__ == "__main__":
    unittest.main()
