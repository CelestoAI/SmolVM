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

import logging
import time
from pathlib import Path
from typing import Any

import requests
import requests_unixsocket

from smolvm.exceptions import FirecrackerAPIError, OperationTimeoutError

logger = logging.getLogger(__name__)


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
        self._session: requests_unixsocket.Session | None = None

    @property
    def session(self) -> requests_unixsocket.Session:
        """Get or create the requests session."""
        if self._session is None:
            self._session = requests_unixsocket.Session()
        return self._session

    def _socket_url(self, path: str) -> str:
        """Build the URL for the Unix socket.

        Args:
            path: API path (e.g., "/boot-source").

        Returns:
            Full URL with socket encoding.
        """
        # requests-unixsocket uses http+unix:// scheme
        socket_encoded = str(self.socket_path).replace("/", "%2F")
        return f"http+unix://{socket_encoded}{path}"

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
        url = self._socket_url(path)
        logger.debug("%s %s", method, path)

        try:
            response = self.session.request(
                method,
                url,
                json=json,
                timeout=10,
            )
        except requests.RequestException as e:
            raise FirecrackerAPIError(f"Request failed: {e}") from e

        if response.status_code not in expected_status:
            error_msg = response.text
            try:
                error_data = response.json()
                error_msg = error_data.get("fault_message", error_msg)
            except Exception:
                pass
            raise FirecrackerAPIError(
                f"API error: {error_msg}",
                status_code=response.status_code,
            )

        if response.status_code == 204:
            return None

        try:
            return response.json()
        except Exception:
            return None

    def wait_for_socket(self, timeout: float = 10.0) -> None:
        """Wait for the Firecracker socket to become available.

        Args:
            timeout: Maximum seconds to wait.

        Raises:
            OperationTimeoutError: If socket doesn't become available.
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.socket_path.exists():
                # Try a simple request to verify it's responsive
                try:
                    self._request("GET", "/", expected_status=(200,))
                    logger.debug("Socket ready: %s", self.socket_path)
                    return
                except Exception:
                    pass
            time.sleep(0.1)

        raise OperationTimeoutError("wait_for_socket", timeout)

    async def async_wait_for_socket(self, timeout: float = 10.0) -> None:
        """Async version of :meth:`wait_for_socket`.

        Replaces blocking ``time.sleep`` with ``asyncio.sleep`` so other
        coroutines can make progress while waiting.
        """
        import asyncio

        start = time.time()
        while time.time() - start < timeout:
            if self.socket_path.exists():
                try:
                    await asyncio.to_thread(
                        self._request, "GET", "/", expected_status=(200,)
                    )
                    logger.debug("Socket ready (async): %s", self.socket_path)
                    return
                except Exception:
                    pass
            await asyncio.sleep(0.1)

        raise OperationTimeoutError("wait_for_socket", timeout)

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
        """Close the session."""
        if self._session is not None:
            self._session.close()
            self._session = None
