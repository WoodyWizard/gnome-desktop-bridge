"""Errors shared by the HTTP API and backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BridgeError(Exception):
    """An error safe to serialize to a local API client."""

    code: str
    message: str
    status: int = 400
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class AccessDenied(BridgeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("access_denied", message, 403, details or {})


class BackendUnavailable(BridgeError):
    def __init__(self, backend: str, message: str) -> None:
        super().__init__(
            "backend_unavailable",
            message,
            503,
            {"backend": backend},
        )


class InvalidRequest(BridgeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("invalid_request", message, 400, details or {})


class NotFound(BridgeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("not_found", message, 404, details or {})


class StaleReference(BridgeError):
    def __init__(self, ref: str) -> None:
        super().__init__(
            "stale_reference",
            "The semantic element reference is stale; request a new snapshot.",
            409,
            {"ref": ref},
        )
