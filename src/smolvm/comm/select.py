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

"""Decide which control channel a VM should use.

Two layers cooperate:

1. ``resolve_comm_channel`` (here) is the *static* decision: given the user's
   request, the VM config, the backend, the guest OS, and whether the host can
   do vsock, it returns the channel to use. It raises a structured error when
   the user explicitly asked for vsock somewhere it can't work; callers format
   the final user-facing recovery message.
2. The facade then does the *runtime* probe: when vsock is the choice it pings
   the guest agent; if the agent does not answer, vsock readiness fails instead
   of silently downgrading to SSH.

vsock uses native ``vhost-vsock-pci`` on QEMU/Linux and Firecracker's
host-side Unix socket bridge on Firecracker/Linux. Windows guests always use
SSH for now.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from smolvm.comm.base import CommChannelKind
from smolvm.exceptions import SmolVMError
from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_QEMU
from smolvm.types import GuestOS

_VHOST_VSOCK_DEV = Path("/dev/vhost-vsock")


def host_supports_vsock() -> bool:
    """Whether this host can provide a QEMU vsock device.

    True only on Linux with the ``vhost_vsock`` driver loaded (its device node
    present). macOS/HVF has no equivalent, so this returns False there.
    """
    return platform.system() == "Linux" and _VHOST_VSOCK_DEV.exists()


@dataclass(frozen=True)
class ChannelResolution:
    """Outcome of channel selection.

    Attributes:
        kind: The channel to use.
    """

    kind: CommChannelKind


class VsockNotSupportedError(SmolVMError):
    """Raised when an explicit vsock request cannot be satisfied."""

    def __init__(
        self,
        code: str,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        details = {"code": code, "reason": reason}
        if metadata:
            details.update(metadata)
        super().__init__(code, details)
        self.code = code
        self.reason = reason


def resolve_comm_channel(
    *,
    requested: CommChannelKind | None,
    config_channel: CommChannelKind | None,
    backend: str | None,
    guest_os: GuestOS | str | None,
    host_vsock_supported: bool | None = None,
) -> ChannelResolution:
    """Resolve the control channel for a VM.

    Args:
        requested: Explicit ``comm_channel`` kwarg from the caller (highest
            precedence).
        config_channel: ``VMConfig.comm_channel`` persisted on the VM.
        backend: Resolved runtime backend (``"qemu"``/``"firecracker"``/...).
        guest_os: The guest operating system.
        host_vsock_supported: Override for :func:`host_supports_vsock`
            (injected in tests). Defaults to probing the host.

    Returns:
        A :class:`ChannelResolution`.

    Raises:
        VsockNotSupportedError: If vsock was explicitly requested but cannot
            work here.
    """
    if host_vsock_supported is None:
        host_vsock_supported = host_supports_vsock()

    is_windows = guest_os == GuestOS.WINDOWS or guest_os == "windows"
    is_qemu = backend == BACKEND_QEMU
    is_firecracker = backend == BACKEND_FIRECRACKER
    host_is_linux = platform.system() == "Linux"
    vsock_possible = not is_windows and (
        (is_qemu and host_vsock_supported) or (is_firecracker and host_is_linux)
    )

    effective = requested if requested is not None else config_channel

    if effective == "ssh":
        return ChannelResolution(kind="ssh")

    if effective == "vsock":
        if is_windows:
            raise VsockNotSupportedError(
                "vsock_not_supported_for_windows",
                "vsock is not available for Windows guests",
                {"guest_os": "windows"},
            )
        if not (is_qemu or is_firecracker):
            raise VsockNotSupportedError(
                "vsock_not_supported_for_backend",
                "vsock is only supported on the QEMU and Firecracker backends in this release",
                {"backend": backend},
            )
        if is_qemu and not host_vsock_supported:
            raise VsockNotSupportedError(
                "vsock_host_device_missing",
                "this host cannot provide QEMU vsock because vhost_vsock is not available",
                {"backend": backend, "required_device": str(_VHOST_VSOCK_DEV)},
            )
        if is_firecracker and not host_is_linux:
            raise VsockNotSupportedError(
                "vsock_not_supported_for_firecracker_host",
                "Firecracker vsock is only available on Linux",
                {"backend": backend, "host_os": platform.system()},
            )
        # Explicit request: use vsock, never silently downgrade to SSH.
        return ChannelResolution(kind="vsock")

    # Auto: prefer vsock where it can work. SmolVM is still alpha, so current
    # images are required to include the Rust guest agent.
    if vsock_possible:
        return ChannelResolution(kind="vsock")
    return ChannelResolution(kind="ssh")
