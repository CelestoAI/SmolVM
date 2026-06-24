"""Public error types raised by smolvm-core wrapper modules."""

from __future__ import annotations

from typing import Any


class SmolVMCoreError(Exception):
    """Base class for smolvm-core library errors."""


class CoreUnavailableError(SmolVMCoreError):
    """Raised when a requested native helper is not available."""


class QMPError(SmolVMCoreError):
    """Raised when QEMU monitor control fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class FirecrackerAPIError(SmolVMCoreError):
    """Raised when the Firecracker API returns an error response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


__all__ = [
    "CoreUnavailableError",
    "FirecrackerAPIError",
    "QMPError",
    "SmolVMCoreError",
]
