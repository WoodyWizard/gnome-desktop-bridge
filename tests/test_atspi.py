from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from gnome_desktop_bridge.atspi_backend import AtspiBackend
from gnome_desktop_bridge.policy import AppIdentity


class EmptyStateSet:
    def get_states(self) -> list[Any]:
        return []


class FakeText:
    def __init__(self, value: str) -> None:
        self.value = value
        self.reads = 0

    def get_character_count(self) -> int:
        return len(self.value)

    def get_text(self, start: int, end: int) -> str:
        self.reads += 1
        return self.value[start:end]


class FakeAccessible:
    def __init__(self, *, role: int, role_name: str, text: str) -> None:
        self.role = role
        self.role_name = role_name
        self.text = FakeText(text)
        self.name_reads = 0
        self.description_reads = 0

    def get_name(self) -> str:
        self.name_reads += 1
        return "secret-name"

    def get_description(self) -> str:
        self.description_reads += 1
        return "secret-description"

    def get_role(self) -> int:
        return self.role

    def get_role_name(self) -> str:
        return self.role_name

    def get_interfaces(self) -> list[str]:
        return ["Text"]

    def get_state_set(self) -> EmptyStateSet:
        return EmptyStateSet()

    def get_child_count(self) -> int:
        return 0

    def get_component_iface(self) -> None:
        return None

    def get_action_iface(self) -> None:
        return None

    def get_text_iface(self) -> FakeText:
        return self.text

    def get_value_iface(self) -> None:
        return None


class AtspiSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = AtspiBackend()
        self.backend._atspi = SimpleNamespace(Role=SimpleNamespace(PASSWORD_TEXT=40))  # noqa: SLF001
        self.backend._generation = 1  # noqa: SLF001
        self.identity = AppIdentity(name="Test")

    def serialize(
        self,
        accessible: FakeAccessible,
        *,
        redact: bool,
        text_budget: int = 256_000,
    ) -> dict[str, Any]:
        node = self.backend._serialize_node(  # noqa: SLF001
            accessible,
            self.identity,
            remaining_depth=0,
            max_nodes=10,
            counter=[0],
            truncated=[False],
            visited=set(),
            include_text=True,
            redact_protected_text=redact,
            text_budget=[text_budget],
        )
        assert node is not None
        return node

    def test_protected_widget_text_is_never_fetched_when_redaction_is_enabled(self) -> None:
        accessible = FakeAccessible(role=40, role_name="password text", text="hunter2")
        node = self.serialize(accessible, redact=True)
        self.assertEqual(node["name"], "[REDACTED]")
        self.assertEqual(node["text"], "[REDACTED]")
        self.assertEqual(accessible.name_reads, 0)
        self.assertEqual(accessible.description_reads, 0)
        self.assertEqual(accessible.text.reads, 0)

    def test_text_respects_global_budget_and_marks_truncation(self) -> None:
        accessible = FakeAccessible(role=1, role_name="entry", text="abcdefghijk")
        node = self.serialize(accessible, redact=True, text_budget=5)
        self.assertEqual(node["text"], "abcde")
        self.assertTrue(node["textTruncated"])


if __name__ == "__main__":
    unittest.main()
