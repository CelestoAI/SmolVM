# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Firecracker API client for SmolVM.

Communicates with the Firecracker hypervisor over its Unix socket API.
"""

import json as json_module
import logging
import os
from pathlib import Path
from typing import Any

from smolvm.exceptions import FirecrackerAPIError, OperationTimeoutError

logger = logging.getLogger(__name__)

_DISABLE_NATIVE_ENV = "SMOLVM_DISABLE_NATIVE_FIRECRACKER_API"
_TRUE_ENV_VALUES = {"1", "true", "yes"}

try:
    from smolvm_core import firecracker as core_firecracker
except ImportError:  # pragma: no cover - optional wheel
    core_firecracker = None  # type: ignore[assignment]


def _core_firecracker_unavailable_reason() -> str | None:
    if os.environ.get(_DISABLE_NATIVE_ENV, "").strip().lower() in _TRUE_ENV_VALUES:
        return (
            f"Firecracker control support is disabled; unset `{_DISABLE_NATIVE_ENV}` and try again."
        )
    if core_firecracker is None:
        return (
            "Firecracker control support is missing; "
            "run `uv sync --reinstall-package smolvm-core` and try again."
        )
    if not hasattr(core_firecracker, "available") or not hasattr(
        core_firecracker, "FirecrackerClient"
    ):
        return (
            "Firecracker control support is missing; "
            "run `uv sync --reinstall-package smolvm-core` and try again."
        )
    try:
        if bool(core_firecracker.available()):
            return None
    except Exception as exc:
        logger.debug("Firecracker native availability check failed: %s", exc)
    return (
        "Firecracker control support is missing; "
        "run `uv sync --reinstall-package smolvm-core` and try again."
    )


def _require_core_firecracker(socket_path: Path) -> Any:
    reason = _core_firecracker_unavailable_reason()
    if reason is not None:
        raise FirecrackerAPIError(reason)
    try:
        return core_firecracker.FirecrackerClient(socket_path)
    except Exception as exc:
        raise FirecrackerAPIError(str(exc)) from exc


def _is_instance_start_request(
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> bool:
    """Return whether a request asks Firecracker to start the microVM."""
    return (
        method.upper() == "PUT"
        and path == "/actions"
        and body is not None
        and body.get("action_type") == "InstanceStart"
    )


def _instance_start_already_succeeded(error_msg: str) -> bool:
    """Return whether a replayed start request found the VM already running."""
    return "not supported after starting the microVM" in error_msg


def _decode_firecracker_error(payload: str | None) -> str:
    error_msg = payload or ""
    try:
        error_data = json_module.loads(error_msg)
    except (TypeError, ValueError, json_module.JSONDecodeError) as exc:
        logger.debug("Could not parse Firecracker error payload, using raw message: %s", exc)
        return error_msg
    if isinstance(error_data, dict):
        return str(error_data.get("fault_message", error_msg))
    return error_msg


def _decode_firecracker_success(payload: str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        decoded = json_module.loads(payload)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


class FirecrackerClient:
    """Client for the Firecracker HTTP API over Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        """Initialize the Firecracker client.

        Args:
            socket_path: Path to the Firecracker API socket.
        """
        if socket_path is None:
            raise ValueError("socket_path cannot be None")

        self.socket_path = socket_path

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected_status: tuple[int, ...] = (200, 204),
    ) -> dict[str, Any] | None:
        """Make an API request.

        Args:
            method: HTTP method (GET, PUT, PATCH).
            path: API path.
            json: Request body.
            expected_status: Expected status codes.

        Returns:
            Response JSON or None for 204.

        Raises:
            FirecrackerAPIError: If request fails.
        """
        logger.debug("%s %s", method, path)
        try:
            status_code, payload = self._core_request(
                method,
                path,
                body=json,
            )
        except OSError as error:
            if _is_instance_start_request(method, path, json):
                return self._handle_instance_start_transport_error(
                    error,
                    method,
                    path,
                    json,
                    expected_status,
                )
            raise FirecrackerAPIError(f"Request failed: {error}") from error

        return self._handle_response(status_code, payload, expected_status)

    def _core_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None,
    ) -> tuple[int, str | None]:
        body_json = json_module.dumps(body, separators=(",", ":")) if body is not None else None
        client = _require_core_firecracker(self.socket_path)
        status_code, payload = client.request_raw(
            method,
            path,
            body_json=body_json,
            timeout=10.0,
        )
        return int(status_code), payload

    def _handle_instance_start_transport_error(
        self,
        native_error: OSError,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        expected_status: tuple[int, ...],
    ) -> dict[str, Any] | None:
        try:
            status_code, payload = self._core_request(method, path, body=body)
        except OSError:
            raise FirecrackerAPIError(f"Request failed: {native_error}") from native_error

        if int(status_code) not in expected_status:
            error_msg = _decode_firecracker_error(payload)
            if _instance_start_already_succeeded(error_msg):
                logger.debug(
                    "Treating replayed InstanceStart as successful after native "
                    "transport error: %s",
                    native_error,
                )
                return None
        return self._handle_response(status_code, payload, expected_status)

    def _handle_response(
        self,
        status_code: int,
        payload: str | None,
        expected_status: tuple[int, ...],
    ) -> dict[str, Any] | None:
        if int(status_code) not in expected_status:
            error_msg = _decode_firecracker_error(payload)
            raise FirecrackerAPIError(
                f"API error: {error_msg}",
                status_code=int(status_code),
            )
        return _decode_firecracker_success(payload)

    def wait_for_socket(self, timeout: float = 10.0) -> None:
        """Wait for the Firecracker socket to become available.

        Args:
            timeout: Maximum seconds to wait.

        Raises:
            OperationTimeoutError: If socket doesn't become available.
        """
        client = _require_core_firecracker(self.socket_path)
        try:
            client.wait_for_socket(timeout)
        except OSError as exc:
            raise OperationTimeoutError("wait_for_socket", timeout) from exc
        logger.debug("Socket ready via native Firecracker API: %s", self.socket_path)

    async def async_wait_for_socket(self, timeout: float = 10.0) -> None:
        """Async version of :meth:`wait_for_socket`.

        Replaces blocking ``time.sleep`` with ``asyncio.sleep`` so other
        coroutines can make progress while waiting.
        """
        import asyncio

        await asyncio.to_thread(self.wait_for_socket, timeout)

    def set_boot_source(
        self,
        kernel_image_path: Path,
        boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off",
    ) -> None:
        """Configure the boot source.

        Args:
            kernel_image_path: Path to the kernel image.
            boot_args: Kernel boot arguments.
        """
        self._request(
            "PUT",
            "/boot-source",
            json={
                "kernel_image_path": str(kernel_image_path),
                "boot_args": boot_args,
            },
        )
        logger.debug("Boot source configured")

    def set_machine_config(
        self,
        vcpu_count: int,
        mem_size_mib: int,
    ) -> None:
        """Configure the machine.

        Args:
            vcpu_count: Number of vCPUs.
            mem_size_mib: Memory size in MiB.
        """
        self._request(
            "PUT",
            "/machine-config",
            json={
                "vcpu_count": vcpu_count,
                "mem_size_mib": mem_size_mib,
            },
        )
        logger.debug("Machine config set: vcpus=%d, mem=%dMiB", vcpu_count, mem_size_mib)

    def add_drive(
        self,
        drive_id: str,
        path_on_host: Path,
        is_root_device: bool = True,
        is_read_only: bool = False,
    ) -> None:
        """Add a block device.

        Args:
            drive_id: Unique drive identifier.
            path_on_host: Path to the disk image.
            is_root_device: Whether this is the root device.
            is_read_only: Whether the drive is read-only.
        """
        self._request(
            "PUT",
            f"/drives/{drive_id}",
            json={
                "drive_id": drive_id,
                "path_on_host": str(path_on_host),
                "is_root_device": is_root_device,
                "is_read_only": is_read_only,
            },
        )
        logger.debug("Drive added: %s", drive_id)

    def add_network_interface(
        self,
        iface_id: str,
        host_dev_name: str,
        guest_mac: str,
        rate_limit_mbps: int | None = None,
    ) -> None:
        """Add a network interface.

        Args:
            iface_id: Interface identifier.
            host_dev_name: TAP device name on host.
            guest_mac: MAC address for the guest.
            rate_limit_mbps: Optional rate limit in Mbps.
        """
        payload: dict[str, Any] = {
            "iface_id": iface_id,
            "host_dev_name": host_dev_name,
            "guest_mac": guest_mac,
        }

        if rate_limit_mbps is not None and rate_limit_mbps > 0:
            bytes_per_sec = rate_limit_mbps * 125000
            # 100ms refill time is standard for smooth throughput
            size_per_100ms = max(bytes_per_sec // 10, 1)

            token_bucket = {
                "bandwidth": {
                    "size": size_per_100ms,
                    "one_time_burst": bytes_per_sec,
                    "refill_time": 100,
                }
            }
            payload["rx_rate_limiter"] = token_bucket
            payload["tx_rate_limiter"] = token_bucket

        self._request(
            "PUT",
            f"/network-interfaces/{iface_id}",
            json=payload,
        )
        logger.debug("Network interface added: %s -> %s", iface_id, host_dev_name)

    def add_vsock_device(
        self,
        guest_cid: int,
        uds_path: str,
        vsock_id: str = "vsock0",
    ) -> None:
        """Add a virtio-vsock device for host↔guest communication.

        Args:
            guest_cid: Context ID for the guest (≥ 3).
            uds_path: Path to the host-side Unix domain socket.
            vsock_id: Device identifier (default: "vsock0").
        """
        self._request(
            "PUT",
            "/vsock",
            json={
                "vsock_id": vsock_id,
                "guest_cid": guest_cid,
                "uds_path": uds_path,
            },
        )
        logger.debug("Vsock device added: CID %d -> %s", guest_cid, uds_path)

    def start_instance(self) -> None:
        """Start the microVM instance."""
        self._request(
            "PUT",
            "/actions",
            json={"action_type": "InstanceStart"},
        )
        logger.info("Instance started")

    def pause_vm(self) -> None:
        """Pause a running microVM."""
        self._request(
            "PATCH",
            "/vm",
            json={"state": "Paused"},
        )
        logger.info("VM paused")

    def resume_vm(self) -> None:
        """Resume a paused microVM."""
        self._request(
            "PATCH",
            "/vm",
            json={"state": "Resumed"},
        )
        logger.info("VM resumed")

    def create_snapshot(
        self,
        snapshot_path: Path,
        mem_file_path: Path,
        snapshot_type: str = "Full",
    ) -> None:
        """Create a Firecracker snapshot."""
        self._request(
            "PUT",
            "/snapshot/create",
            json={
                "snapshot_path": str(snapshot_path),
                "mem_file_path": str(mem_file_path),
                "snapshot_type": snapshot_type,
            },
        )
        logger.info("Snapshot created: %s", snapshot_path)

    def load_snapshot(
        self,
        snapshot_path: Path,
        mem_backend_path: Path,
        *,
        backend_type: str = "File",
        resume_vm: bool = False,
        track_dirty_pages: bool | None = None,
        network_overrides: list[dict[str, str]] | None = None,
    ) -> None:
        """Load a Firecracker snapshot before boot."""
        payload: dict[str, Any] = {
            "snapshot_path": str(snapshot_path),
            "mem_backend": {
                "backend_path": str(mem_backend_path),
                "backend_type": backend_type,
            },
            "resume_vm": resume_vm,
        }
        if track_dirty_pages is not None:
            payload["track_dirty_pages"] = track_dirty_pages
        if network_overrides:
            payload["network_overrides"] = network_overrides

        self._request(
            "PUT",
            "/snapshot/load",
            json=payload,
        )
        logger.info("Snapshot loaded: %s", snapshot_path)

    def get_instance_info(self) -> dict[str, Any]:
        """Get instance information.

        Returns:
            Instance info dictionary.
        """
        result = self._request("GET", "/")
        return result or {}

    def send_ctrl_alt_del(self) -> None:
        """Send Ctrl+Alt+Del to gracefully shutdown the guest."""
        self._request(
            "PUT",
            "/actions",
            json={"action_type": "SendCtrlAltDel"},
        )
        logger.info("Sent Ctrl+Alt+Del")

    def close(self) -> None:
        """Close the client."""
        return None
