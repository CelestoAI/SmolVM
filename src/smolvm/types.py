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

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


class VMState(str, Enum):
    """VM lifecycle states."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class BrowserSessionState(str, Enum):
    """Browser session lifecycle states."""

    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"


class GuestOS(str, Enum):
    """Supported guest operating systems for auto-configured VMs."""

    ALPINE = "alpine"
    DEBIAN = "debian"
    UBUNTU = "ubuntu"


def _generate_vm_id() -> str:
    """Generate a VM identifier compatible with VMConfig validation."""
    return f"vm-{uuid4().hex[:8]}"


def _generate_browser_session_id() -> str:
    """Generate a browser session identifier."""
    return f"browser-{uuid4().hex[:8]}"


def _generate_snapshot_id() -> str:
    """Generate a snapshot identifier."""
    return f"snap-{uuid4().hex[:8]}"


_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$"


class BrowserViewport(BaseModel):
    """Viewport settings for browser sessions."""

    width: Annotated[int, Field(ge=640, le=7680)] = 1280
    height: Annotated[int, Field(ge=480, le=4320)] = 720

    model_config = {"frozen": True}


class PortForwardConfig(BaseModel):
    """Host-to-guest TCP port forwarding configuration."""

    host_port: Annotated[int, Field(ge=1, le=65535)]
    guest_port: Annotated[int, Field(ge=1, le=65535)]
    host_address: str = "127.0.0.1"

    model_config = {"frozen": True}


class VsockConfig(BaseModel):
    """Virtio-vsock device configuration for host↔guest communication.

    Vsock provides a direct, high-performance communication channel between
    the host and guest without requiring network configuration. On the host
    side, Firecracker exposes the vsock as a Unix domain socket (UDS). The
    guest connects via ``AF_VSOCK`` sockets using the assigned CID.

    Attributes:
        guest_cid: Context ID for the guest. Must be ≥ 3 (0 = hypervisor,
            1 = reserved, 2 = host). Each VM must have a unique CID.
        uds_path: Path to the Unix domain socket on the host. If ``None``,
            SmolVM auto-generates one in the socket directory.
    """

    guest_cid: Annotated[int, Field(ge=3, le=4294967295)]
    uds_path: str | None = None

    model_config = {"frozen": True}


class InternetSettings(BaseModel):
    """Network access controls for a VM.

    Restricts which external domains (and eventually HTTP methods) the guest
    can reach.  Domain entries may be full URLs (``https://example.com/path``)
    or bare hostnames (``example.com``).  The special value ``"*"`` means
    "allow everything" (the default).

    Attributes:
        allowed_domains: Domains the guest may connect to.
            Accepts URLs or bare hostnames.  Default ``["*"]`` allows all.
        allowed_http_methods: HTTP methods the guest may use.
            Default ``["*"]`` allows all.  **Not enforced yet** — reserved
            for a future proxy-based implementation.
    """

    allowed_domains: list[str] = ["*"]
    allowed_http_methods: list[str] = ["*"]

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, v: list[str]) -> list[str]:
        """Extract and store only lowercased hostnames."""
        normalized: list[str] = []
        for entry in v:
            entry = entry.strip()
            if not entry:
                continue
            if entry == "*":
                normalized.append(entry)
                continue
            if "://" in entry:
                parsed = urlparse(entry)
                if parsed.username or parsed.password:
                    raise ValueError(
                        f"allowed_domains entries must not contain credentials: {entry!r}"
                    )
                if parsed.path and parsed.path != "/":
                    raise ValueError(
                        f"allowed_domains entries must be hostnames, not URLs with paths: {entry!r}"
                    )
                if parsed.query or parsed.fragment or parsed.params:
                    raise ValueError(
                        f"allowed_domains entries must be hostnames, "
                        f"not URLs with query/fragment: {entry!r}"
                    )
                hostname = parsed.hostname
                if not hostname:
                    raise ValueError(f"Could not extract hostname from: {entry!r}")
                normalized.append(hostname.lower())
            else:
                # Bare hostname, possibly with a port like "example.com:8080"
                hostname = entry.split(":")[0]
                normalized.append(hostname.lower())
        if not normalized:
            raise ValueError("allowed_domains must contain at least one entry")
        return normalized

    @field_validator("allowed_http_methods")
    @classmethod
    def normalize_methods(cls, v: list[str]) -> list[str]:
        """Uppercase and deduplicate HTTP method entries."""
        normalized: list[str] = []
        seen: set[str] = set()
        for method in v:
            method = method.strip().upper()
            if not method:
                continue
            if method in seen:
                continue
            seen.add(method)
            normalized.append(method)
        if not normalized:
            raise ValueError("allowed_http_methods must contain at least one entry")
        return normalized

    @property
    def is_allow_all_domains(self) -> bool:
        """Whether all domains are allowed (wildcard)."""
        return "*" in self.allowed_domains

    model_config = {"frozen": True}


class VMConfig(BaseModel):
    """Configuration for creating a microVM.

    Attributes:
        vm_id: Optional unique identifier (lowercase alphanumeric with hyphens).
            If omitted, SmolVM auto-generates one.
        vcpu_count: Number of virtual CPUs (1-32).
        mem_size_mib: Memory size in MiB (128-16384).
        kernel_path: Path to the kernel image.
        initrd_path: Optional path to the initrd image.
        rootfs_path: Path to the root filesystem image.
        extra_drives: Additional block-device image paths to attach at boot.
        boot_args: Kernel boot arguments.
        ssh_capable: Whether this boot path is expected to start guest SSH
            without relying on ``init=/init``.
        backend: Optional runtime backend override ("firecracker", "qemu", or "libkrun").
        disk_mode: Disk lifecycle mode:
            - ``"isolated"`` (default): clone rootfs per VM for sandbox isolation.
            - ``"shared"``: boot directly from ``rootfs_path``.
        retain_disk_on_delete: Keep isolated VM disk after delete, so a later
            create with the same VM ID can reuse prior state.
        env_vars: Environment variables to inject into the guest
            after boot via SSH. Keys must be valid shell identifiers.
        port_forwards: Optional host TCP forwards configured at VM launch.
    """

    vm_id: Annotated[
        str,
        Field(
            default_factory=_generate_vm_id,
            pattern=_IDENTIFIER_PATTERN,
        ),
    ]
    vcpu_count: Annotated[int, Field(ge=1, le=32)] = 2
    mem_size_mib: Annotated[int, Field(ge=128, le=16384)] = 512
    kernel_path: Path
    initrd_path: Path | None = None
    rootfs_path: Path
    extra_drives: list[Path] = []
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    ssh_capable: bool = False
    backend: str | None = None
    disk_mode: Literal["isolated", "shared"] = "isolated"
    retain_disk_on_delete: bool = False
    env_vars: dict[str, str] = {}
    network_rate_limit_mbps: Annotated[int, Field(ge=1)] | None = None
    port_forwards: list[PortForwardConfig] = []
    vsock: VsockConfig | None = None
    internet_settings: InternetSettings | None = None

    @field_validator("vm_id", mode="before")
    @classmethod
    def default_vm_id_when_none(cls, v: object) -> object:
        """Generate VM ID when explicitly provided as ``None``."""
        if v is None:
            return _generate_vm_id()
        return v

    @field_validator("kernel_path", "rootfs_path")
    @classmethod
    def validate_path_exists(cls, v: Path, info: ValidationInfo) -> Path:
        """Ensure paths exist on the filesystem."""
        if not cls._should_validate_paths(info):
            return v
        return cls._validate_file_path(v)

    @field_validator("initrd_path")
    @classmethod
    def validate_optional_path_exists(cls, v: Path | None) -> Path | None:
        """Ensure optional paths exist on the filesystem."""
        if v is None:
            return None
        return cls._validate_file_path(v)

    @field_validator("extra_drives")
    @classmethod
    def validate_extra_drives(cls, v: list[Path], info: ValidationInfo) -> list[Path]:
        """Ensure all extra drive paths exist and are files."""
        if not cls._should_validate_paths(info):
            return v
        for path in v:
            cls._validate_file_path(path)
        return v

    @staticmethod
    def _validate_file_path(v: Path) -> Path:
        """Validate a filesystem path points to an existing file."""
        if not v.exists():
            raise ValueError(f"Path does not exist: {v}")
        if not v.is_file():
            raise ValueError(f"Path is not a file: {v}")
        return v

    @staticmethod
    def _should_validate_paths(info: ValidationInfo) -> bool:
        """Allow storage reads to skip filesystem existence checks."""
        return bool((info.context or {}).get("validate_paths", True))

    @field_validator("env_vars")
    @classmethod
    def validate_env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        """Ensure all env var keys are valid shell identifiers."""
        from smolvm.env import validate_env_key  # deferred to avoid circular import

        for key in v:
            validate_env_key(key)
        return v

    @field_validator("port_forwards")
    @classmethod
    def validate_port_forwards(cls, v: list[PortForwardConfig]) -> list[PortForwardConfig]:
        """Ensure port-forward definitions do not reuse host or guest ports."""
        seen_host_ports: set[int] = set()
        seen_guest_ports: set[int] = set()
        for forward in v:
            if forward.host_port in seen_host_ports:
                raise ValueError(f"Duplicate host port in port_forwards: {forward.host_port}")
            if forward.guest_port in seen_guest_ports:
                raise ValueError(f"Duplicate guest port in port_forwards: {forward.guest_port}")
            seen_host_ports.add(forward.host_port)
            seen_guest_ports.add(forward.guest_port)
        return v

    model_config = {"frozen": True}


class BrowserSessionConfig(BaseModel):
    """Configuration for launching a browser session."""

    session_id: Annotated[
        str | None,
        Field(default=None, pattern=_IDENTIFIER_PATTERN),
    ] = None
    backend: Literal["firecracker", "qemu", "libkrun", "auto"] = "auto"
    browser: Literal["chromium"] = "chromium"
    mode: Literal["headless", "live"] = "headless"
    profile_mode: Literal["ephemeral", "persistent"] = "ephemeral"
    profile_id: Annotated[
        str | None,
        Field(default=None, pattern=_IDENTIFIER_PATTERN),
    ] = None
    timeout_minutes: Annotated[int, Field(ge=1, le=240)] = 30
    viewport_width: Annotated[int, Field(ge=640, le=7680)] = 1280
    viewport_height: Annotated[int, Field(ge=480, le=4320)] = 720
    viewport: BrowserViewport | None = None
    record_video: bool = False
    allow_downloads: bool = True
    network_policy_id: str | None = None
    env_vars: dict[str, str] = {}
    mem_size_mib: Annotated[int, Field(ge=512, le=16384)] = 2048
    disk_size_mib: Annotated[int, Field(ge=2048, le=16384)] = 4096

    @model_validator(mode="before")
    @classmethod
    def normalize_viewport(cls, raw: Any) -> Any:
        """Allow callers to specify viewport via a nested object."""
        if not isinstance(raw, dict):
            return raw

        data = dict(raw)
        viewport = data.get("viewport")
        if viewport is None:
            data["viewport"] = {
                "width": data.get("viewport_width", 1280),
                "height": data.get("viewport_height", 720),
            }
            return data

        if isinstance(viewport, BrowserViewport):
            width = viewport.width
            height = viewport.height
        elif isinstance(viewport, dict):
            width = viewport.get("width")
            height = viewport.get("height")
        else:
            raise ValueError("viewport must be a mapping with width/height values")

        data.setdefault("viewport_width", width)
        data.setdefault("viewport_height", height)
        return data

    @model_validator(mode="after")
    def validate_browser_session_config(self) -> "BrowserSessionConfig":
        """Validate cross-field browser session constraints."""
        if self.viewport is None:
            raise ValueError("viewport could not be resolved")

        if self.viewport.width != self.viewport_width:
            raise ValueError("viewport.width must match viewport_width")
        if self.viewport.height != self.viewport_height:
            raise ValueError("viewport.height must match viewport_height")

        if self.profile_mode == "persistent" and not self.profile_id:
            raise ValueError("profile_id is required when profile_mode='persistent'")

        if self.record_video and self.mode != "live":
            raise ValueError("record_video requires mode='live'")

        if self.network_policy_id is not None and not self.network_policy_id.strip():
            raise ValueError("network_policy_id cannot be empty")

        return self

    @field_validator("session_id", "profile_id", "network_policy_id")
    @classmethod
    def strip_optional_identifiers(cls, value: str | None) -> str | None:
        """Normalize optional identifier-like strings."""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized

    @field_validator("env_vars")
    @classmethod
    def validate_env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        """Ensure all env var keys are valid shell identifiers."""
        from smolvm.env import validate_env_key  # deferred to avoid circular import

        for key in v:
            validate_env_key(key)
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
        pid: Process ID of the VM process (if running).
        control_socket_path: Path to the runtime control socket.
    """


    vm_id: str
    status: VMState
    config: VMConfig
    network: NetworkConfig | None = None
    pid: int | None = None
    control_socket_path: Path | None = None
    vsock_uds_path: Path | None = None

    model_config = {"frozen": True}


class SnapshotArtifacts(BaseModel):
    """Filesystem artifacts associated with a persisted VM snapshot."""

    state_path: Path | None = None
    memory_path: Path | None = None
    disk_path: Path

    model_config = {"frozen": True}


class SnapshotInfo(BaseModel):
    """Persisted metadata for a VM snapshot."""

    snapshot_id: Annotated[
        str,
        Field(default_factory=_generate_snapshot_id, pattern=_IDENTIFIER_PATTERN),
    ]
    vm_id: Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
    backend: Literal["firecracker", "qemu", "libkrun"]
    artifacts: SnapshotArtifacts
    vm_config: VMConfig
    network_config: NetworkConfig
    created_at: datetime
    restored: bool = False
    restored_vm_id: str | None = None

    model_config = {"frozen": True}




class BrowserSessionInfo(BaseModel):
    """Runtime information about a browser session."""

    session_id: str
    vm_id: str
    status: BrowserSessionState
    cdp_url: str | None = None
    live_url: str | None = None
    debug_port: int | None = None
    profile_id: str | None = None
    expires_at: datetime | None = None
    artifacts_dir: Path | None = None

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
