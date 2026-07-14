from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any

from gnome_desktop_bridge.events import EventBuffer
from gnome_desktop_bridge.server import BridgeHandler, BridgeHTTPServer


class FakeDispatcher:
    def state(self) -> dict[str, Any]:
        return {"accessMode": "off"}

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"received": payload}


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
