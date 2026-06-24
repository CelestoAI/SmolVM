"""Firecracker API control over a Unix socket."""

from __future__ import annotations

import json as json_module
from pathlib import Path
from typing import Any

from . import _ffi
from .errors import CoreUnavailableError, FirecrackerAPIError


def available() -> bool:
    """Return True when this wheel includes Firecracker API control."""

    return bool(_ffi.has_native_firecracker_api())


def _decode_error(payload: str | None) -> str:
    error_msg = payload or ""
    try:
        error_data = json_module.loads(error_msg)
    except (TypeError, ValueError, json_module.JSONDecodeError):
        return error_msg
    if isinstance(error_data, dict):
        return str(error_data.get("fault_message", error_msg))
    return error_msg


def _decode_success(payload: str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        decoded = json_module.loads(payload)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


class FirecrackerClient:
    """Client for Firecracker's HTTP API over a Unix socket."""

    def __init__(self, socket_path: Path | str) -> None:
        if socket_path is None:
            raise ValueError("socket_path cannot be None")
        if not available():
            raise CoreUnavailableError(
                "Firecracker control support is missing; "
                "run `uv sync --reinstall-package smolvm-core` and try again."
            )
        self.socket_path = Path(socket_path)

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        body_json: str | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, str | None]:
        """Send one API request and return the raw status and payload."""

        status_code, payload = _ffi.firecracker_request(
            str(self.socket_path),
            method,
            path,
            body_json,
            timeout,
        )
        return int(status_code), payload

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        expected_statuses: tuple[int, ...] = (200, 204),
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Send one API request and raise when Firecracker returns an error."""

        body_json = json_module.dumps(body, separators=(",", ":")) if body is not None else None
        try:
            status_code, payload = self.request_raw(
                method,
                path,
                body_json=body_json,
                timeout=timeout,
            )
        except OSError as exc:
            raise FirecrackerAPIError(
                f"Could not reach Firecracker API socket at {self.socket_path}: {exc}"
            ) from exc
        if status_code not in expected_statuses:
            raise FirecrackerAPIError(
                f"API error: {_decode_error(payload)}",
                status_code=status_code,
            )
        return _decode_success(payload)

    def wait_for_socket(self, timeout: float = 10.0) -> None:
        """Wait for the Firecracker API socket to accept requests."""

        _ffi.firecracker_wait_for_socket(str(self.socket_path), timeout)


__all__ = [
    "FirecrackerClient",
    "available",
]
