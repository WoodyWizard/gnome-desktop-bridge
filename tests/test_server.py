from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any

from gnome_desktop_bridge.events import EventBuffer
from gnome_desktop_bridge.server import BridgeHandler, BridgeHTTPServer


class FakeDispatcher:
    def state(self) -> dict[str, Any]:
        return {"accessMode": "off"}

    def dispatch(self, payload: dict[str, Any], *, client: str = "api") -> dict[str, Any]:
        return {"received": payload, "client": client}


class ServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = "a" * 64
        cls.events = EventBuffer()
        try:
            cls.server = BridgeHTTPServer(
                ("127.0.0.1", 0),
                BridgeHandler,
                token=cls.token,
                dispatcher=FakeDispatcher(),  # type: ignore[arg-type]
                events=cls.events,
            )
        except PermissionError as exc:
            raise unittest.SkipTest(f"sandbox does not allow a loopback listener: {exc}") from exc
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=2)

    def _open(
        self,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        host: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if host is not None:
            headers["Host"] = host
        data = None
        method = "GET"
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            finally:
                exc.close()

    def test_health_is_public_but_minimal(self) -> None:
        status, payload = self._open("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("accessMode", payload)

    def test_api_rejects_missing_token(self) -> None:
        status, payload = self._open("/api/status")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_authenticated_command_round_trip(self) -> None:
        command = {"action": "ping", "args": {}}
        status, payload = self._open(
            "/api/command",
            token=self.token,
            body=command,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["received"], command)

    def test_client_header_is_forwarded_and_sanitized(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/api/command",
            data=json.dumps({"action": "ping"}).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-Bridge-Client": "Claude",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(json.load(response)["result"]["client"], "Claude")

    def test_non_ascii_authorization_is_rejected_cleanly(self) -> None:
        status, payload = self._open("/api/status", token="\u00e9" * 64)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_non_finite_json_numbers_are_rejected(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/api/command",
            data=b'{"action": "pointer_move", "args": {"x": NaN, "y": 1}}',
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_event_stream_counts_overlay_until_the_client_disconnects(self) -> None:
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        connection.sendall(
            (
                "GET /api/events/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {self.token}\r\nX-Bridge-Client: overlay\r\n\r\n"
            ).encode()
        )
        self.assertIn(b"200", connection.recv(1024))
        self.assertEqual(self.events.subscriber_count("overlay"), 1)
        connection.close()
        deadline = time.monotonic() + 3
        while self.events.subscriber_count("overlay") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.events.subscriber_count("overlay"), 0)

    def test_api_rejects_dns_rebinding_host(self) -> None:
        status, payload = self._open(
            "/api/status",
            token=self.token,
            host="attacker.invalid",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_host")


if __name__ == "__main__":
    unittest.main()
