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

"""Core types and Pydantic models for SmolVM SDK."""

from enum import Enum
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class VMState(str, Enum):
    """VM lifecycle states."""

    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


def _generate_vm_id() -> str:
    """Generate a VM identifier compatible with VMConfig validation."""
    return f"vm-{uuid4().hex[:8]}"


class VMConfig(BaseModel):
    """Configuration for creating a microVM.

    Attributes:
        vm_id: Optional unique identifier (lowercase alphanumeric with hyphens).
            If omitted, SmolVM auto-generates one.
        vcpu_count: Number of virtual CPUs (1-32).
        mem_size_mib: Memory size in MiB (128-16384).
        kernel_path: Path to the kernel image.
        rootfs_path: Path to the root filesystem image.
        boot_args: Kernel boot arguments.
        backend: Optional runtime backend override ("firecracker" or "qemu").
    """

    vm_id: Annotated[
        str,
        Field(
            default_factory=_generate_vm_id,
            pattern=r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$",
        ),
    ]
    vcpu_count: Annotated[int, Field(ge=1, le=32)] = 2
    mem_size_mib: Annotated[int, Field(ge=128, le=16384)] = 512
    kernel_path: Path
    rootfs_path: Path
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    backend: str | None = None

    @field_validator("vm_id", mode="before")
    @classmethod
    def default_vm_id_when_none(cls, v: object) -> object:
        """Generate VM ID when explicitly provided as ``None``."""
        if v is None:
            return _generate_vm_id()
        return v

    @field_validator("kernel_path", "rootfs_path")
    @classmethod
    def validate_path_exists(cls, v: Path) -> Path:
        """Ensure paths exist on the filesystem."""
        if not v.exists():
            raise ValueError(f"Path does not exist: {v}")
        if not v.is_file():
            raise ValueError(f"Path is not a file: {v}")
        return v

    model_config = {"frozen": True}


class NetworkConfig(BaseModel):
    """Network configuration for a VM.

    Attributes:
        guest_ip: IP address assigned to the guest.
        gateway_ip: Gateway IP (host side of TAP).
        netmask: Network mask.
        tap_device: Name of the TAP device.
        guest_mac: MAC address for the guest interface.
        ssh_host_port: Optional host TCP port forwarded to guest SSH (22).
    """

    guest_ip: str
    gateway_ip: str = "172.16.0.1"
    netmask: str = "255.255.255.0"
    tap_device: str
    guest_mac: str
    ssh_host_port: int | None = None

    model_config = {"frozen": True}


class VMInfo(BaseModel):
    """Runtime information about a VM.

    Attributes:
        vm_id: The VM identifier.
        status: Current lifecycle state.
        config: The VM configuration.
        network: Network configuration.
        pid: Process ID of the Firecracker process (if running).
        socket_path: Path to the Firecracker API socket.
    """

    vm_id: str
    status: VMState
    config: VMConfig
    network: NetworkConfig | None = None
    pid: int | None = None
    socket_path: Path | None = None

    model_config = {"frozen": True}


class CommandResult(BaseModel):
    """Result of executing a command on a guest VM.

    Attributes:
        exit_code: Exit code of the command (0 = success).
        stdout: Standard output captured from the command.
        stderr: Standard error captured from the command.
    """

    exit_code: int
    stdout: str
    stderr: str

    model_config = {"frozen": True}

    @property
    def ok(self) -> bool:
        """Whether the command succeeded (exit_code == 0)."""
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Convenience alias for stripped standard output."""
        return self.stdout.strip()
