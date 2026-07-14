from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gnome_desktop_bridge.portal_backend import PortalBackend

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


if __name__ == "__main__":
    unittest.main()
