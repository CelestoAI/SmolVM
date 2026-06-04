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

from smolvm._naming import generate_sandbox_name


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
    UBUNTU = "ubuntu"
    WINDOWS = "windows"


class SnapshotType(str, Enum):
    """How much of a VM's disk a snapshot stores.

    ``FULL`` (the default) writes a complete, self-contained copy of the
    disk, so the snapshot can be restored on its own. ``DIFF`` stores only
    the data that changed since the shared base image, which is much smaller
    but means the snapshot depends on that base image still being present.

    ``DISK`` is like ``FULL`` (self-contained) but stores **only the disk** —
    it does not save the guest's RAM (vmstate). Restoring a ``DISK`` snapshot
    boots the guest fresh from that disk rather than resuming the exact running
    state. Because it skips the RAM dump, taking a ``DISK`` snapshot is far
    faster, never blocks the QEMU monitor, and uses far less disk — the right
    choice when you only need the filesystem state and restore-as-cold-boot is
    acceptable (e.g. sandbox suspend/restore across hosts).
    """

    FULL = "full"
    DIFF = "diff"
    DISK = "disk"


def _generate_vm_id() -> str:
    """Generate a VM identifier compatible with VMConfig validation."""
    return generate_sandbox_name()


def _generate_browser_session_id() -> str:
    """Generate a browser session identifier."""
    return f"browser-{uuid4().hex[:8]}"


def _generate_snapshot_id() -> str:
    """Generate a snapshot identifier."""
    return f"snap-{uuid4().hex[:8]}"


_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$"


def _should_validate_paths(info: ValidationInfo) -> bool:
    """Return whether path-existence checks should run for this validation.

    Storage reads (``vm_config_from_json``) pass ``validate_paths=False`` in
    the validation context so a stale or missing host path on disk does not
    blow up read-only commands like ``smolvm list``. Defaults to ``True`` so
    direct construction (creates, tests) keeps its safety net.
    """
    return bool((info.context or {}).get("validate_paths", True))


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


class WorkspaceMount(BaseModel):
    """Host directory to mount inside the guest via virtio-9p.

    By default the host directory is exposed read-only through QEMU's
    virtio-9p passthrough, with an overlayfs layer on top so the guest
    can read and write freely — changes stay inside the VM and never
    touch the host.

    When ``writable`` is True the host directory is exposed read-write
    and mounted directly at ``guest_path`` (no overlay), so writes from
    the guest are visible on the host.

    Attributes:
        host_path: Absolute path to a directory on the host.
        guest_path: Mount point inside the guest (default ``/workspace``).
        mount_tag: 9p mount tag passed to QEMU.  Auto-generated when omitted.
        writable: When True, guest writes propagate to the host directory.
            Default False (read-only host, writable overlay in guest).
    """

    host_path: Path
    guest_path: str = "/workspace"
    mount_tag: str | None = None
    writable: bool = False

    @field_validator("host_path")
    @classmethod
    def validate_host_path(cls, v: Path, info: ValidationInfo) -> Path:
        """Ensure the host path exists and is a directory.

        Existence and directory checks are skipped when the validation
        context has ``validate_paths=False`` so persisted configs with
        stale mount paths still load (read-only commands surface them as
        warnings instead of crashing).
        """
        v = v.resolve()
        if not _should_validate_paths(info):
            return v
        if not v.exists():
            raise ValueError(f"Workspace path does not exist: {v}")
        if not v.is_dir():
            raise ValueError(f"Workspace path is not a directory: {v}")
        return v

    @field_validator("guest_path")
    @classmethod
    def validate_guest_path(cls, v: str) -> str:
        """Ensure the guest mount point is an absolute path."""
        if not v.startswith("/"):
            raise ValueError(f"guest_path must be an absolute path, got: {v!r}")
        return v

    def resolved_tag(self, index: int) -> str:
        """Return the mount tag, falling back to ``workspace{index}``."""
        return self.mount_tag or f"workspace{index}"

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
        memory: Memory size in MiB (128-16384).
        boot_mode: How the guest boots:

            - ``"direct_kernel"`` (default): the hypervisor loads
              ``kernel_path`` (and optionally ``initrd_path``) directly and
              passes ``boot_args`` as the kernel command line. Required for
              firecracker, libkrun, and microvm-style QEMU boots.
            - ``"firmware"``: QEMU boots the rootfs disk via its default
              firmware (OVMF on aarch64, SeaBIOS on x86_64). The guest kernel
              lives inside ``rootfs_path`` (e.g. a Debian or Ubuntu cloud
              image). ``kernel_path`` must be ``None`` in this mode, and the
              backend must be ``"qemu"``.

        kernel_path: Path to the kernel image. Required when
            ``boot_mode == "direct_kernel"``; must be ``None`` when
            ``boot_mode == "firmware"``.
        initrd_path: Optional path to the initrd image.
        rootfs_path: Path to the root filesystem image.
        extra_drives: Additional block-device image paths to attach at boot.
        boot_args: Kernel boot arguments (ignored in firmware mode).
        ssh_capable: Whether this boot path is expected to start guest SSH
            without relying on ``init=/init``.
        backend: Optional runtime backend override ("firecracker", "qemu", or "libkrun").
        qemu_network: QEMU backend networking mode — ``"slirp"`` (default,
            userspace NAT + host port forwards) or ``"tap"`` (host TAP device
            under the shared nftables NAT/isolation rules). Ignored by non-QEMU
            backends.
        disk_mode: Disk lifecycle mode:
            - ``"isolated"`` (default): clone rootfs per VM for sandbox isolation.
            - ``"shared"``: boot directly from ``rootfs_path``.
        retain_disk_on_delete: Keep isolated VM disk after delete, so a later
            create with the same VM ID can reuse prior state.
        env_vars: Environment variables to inject into the guest
            after boot via SSH. Keys must be valid shell identifiers.
        port_forwards: Optional host TCP forwards configured at VM launch.
        comm_channel: Host↔guest control transport (``"ssh"`` or ``"vsock"``).
            ``None`` means auto-select at runtime (vsock when the guest agent
            answers on a supported backend, else SSH). See
            :func:`smolvm.comm.select.resolve_comm_channel`.
        ssh_public_key: Optional OpenSSH public key (one-line ``authorized_keys``
            format) to install in the guest's ``/root/.ssh/authorized_keys`` at
            first boot. Passed via the kernel command line as
            ``smolvm.authorized_key_b64=<base64>`` and read by ``/init``. Use
            this for published pre-built images that don't bake keys at build
            time, so each VM gets the launching user's key without rebuilding.
    """

    vm_id: Annotated[
        str,
        Field(
            default_factory=_generate_vm_id,
            pattern=_IDENTIFIER_PATTERN,
        ),
    ]
    vcpu_count: Annotated[int, Field(ge=1, le=32)] = 2
    memory: Annotated[int, Field(ge=128, le=16384)] = 512
    guest_os: GuestOS = GuestOS.ALPINE
    boot_mode: Literal["direct_kernel", "firmware"] = "direct_kernel"
    kernel_path: Path | None = None
    initrd_path: Path | None = None
    rootfs_path: Path
    extra_drives: list[Path] = []
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    ssh_capable: bool = False
    backend: str | None = None
    qemu_network: Literal["slirp", "tap"] = "slirp"
    disk_mode: Literal["isolated", "shared"] = "isolated"
    retain_disk_on_delete: bool = False
    env_vars: dict[str, str] = {}
    network_rate_limit_mbps: Annotated[int, Field(ge=1)] | None = None
    port_forwards: list[PortForwardConfig] = []
    vsock: VsockConfig | None = None
    comm_channel: Literal["ssh", "vsock"] | None = None
    internet_settings: InternetSettings | None = None
    workspace_mounts: list[WorkspaceMount] = []
    ssh_public_key: str | None = None

    @field_validator("vm_id", mode="before")
    @classmethod
    def default_vm_id_when_none(cls, v: object) -> object:
        """Generate VM ID when explicitly provided as ``None``."""
        if v is None:
            return _generate_vm_id()
        return v

    @field_validator("kernel_path", "rootfs_path")
    @classmethod
    def validate_path_exists(cls, v: Path | None, info: ValidationInfo) -> Path | None:
        """Ensure paths exist on the filesystem."""
        if v is None:
            return None
        if not _should_validate_paths(info):
            return v
        return cls._validate_file_path(v)

    @model_validator(mode="after")
    def _check_boot_mode_consistency(self) -> "VMConfig":
        """Enforce boot_mode <-> kernel_path <-> backend invariants."""
        if self.boot_mode == "firmware":
            if self.kernel_path is not None:
                raise ValueError(
                    "boot_mode='firmware' requires kernel_path=None "
                    "(the guest kernel is inside the rootfs disk)"
                )
            if self.backend != "qemu":
                raise ValueError(
                    f"boot_mode='firmware' requires backend='qemu' "
                    f"(got backend={self.backend!r}); firmware boot is only "
                    "supported on the QEMU backend, so the caller must set it "
                    "explicitly rather than relying on auto-detection"
                )
        else:  # direct_kernel
            if self.kernel_path is None:
                raise ValueError("boot_mode='direct_kernel' requires kernel_path to be set")
        if self.guest_os is GuestOS.WINDOWS and self.boot_mode != "firmware":
            # Windows always boots via OVMF firmware reading the qcow2's UEFI
            # boot manager; there is no direct-kernel path. The firmware-mode
            # invariants above then also pin backend='qemu' and kernel_path=None.
            raise ValueError(
                f"VM {self.vm_id!r}: guest_os='windows' requires "
                "boot_mode='firmware' (Windows has no direct-kernel boot path)."
            )
        return self

    @field_validator("initrd_path")
    @classmethod
    def validate_optional_path_exists(
        cls,
        v: Path | None,
        info: ValidationInfo,
    ) -> Path | None:
        """Ensure optional paths exist on the filesystem."""
        if v is None:
            return None
        if not _should_validate_paths(info):
            return v
        return cls._validate_file_path(v)

    @field_validator("extra_drives")
    @classmethod
    def validate_extra_drives(cls, v: list[Path], info: ValidationInfo) -> list[Path]:
        """Ensure all extra drive paths exist and are files."""
        if not _should_validate_paths(info):
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

    @field_validator("workspace_mounts")
    @classmethod
    def validate_workspace_mounts(
        cls,
        v: list[WorkspaceMount],
    ) -> list[WorkspaceMount]:
        """Ensure workspace mount tags and guest paths are unique."""
        seen_tags: set[str] = set()
        seen_guest_paths: set[str] = set()
        for index, mount in enumerate(v):
            tag = mount.resolved_tag(index)
            if tag in seen_tags:
                raise ValueError(f"Duplicate workspace mount tag: {tag}")
            if mount.guest_path in seen_guest_paths:
                raise ValueError(f"Duplicate workspace guest_path: {mount.guest_path}")
            seen_tags.add(tag)
            seen_guest_paths.add(mount.guest_path)
        return v

    model_config = {"frozen": True, "extra": "forbid"}


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
    workspace_mounts: list[WorkspaceMount] = []
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

        seen_tags: set[str] = set()
        seen_guest_paths: set[str] = set()
        for index, mount in enumerate(self.workspace_mounts):
            tag = mount.resolved_tag(index)
            if tag in seen_tags:
                raise ValueError(f"Duplicate workspace mount tag: {tag}")
            if mount.guest_path in seen_guest_paths:
                raise ValueError(f"Duplicate workspace guest_path: {mount.guest_path}")
            seen_tags.add(tag)
            seen_guest_paths.add(mount.guest_path)

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
    snapshot_type: SnapshotType = SnapshotType.FULL
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
