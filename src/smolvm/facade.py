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

"""User-friendly VM facade matching the SmolVM Initial Design API.

Provides a simple ``SmolVM`` class that wraps the lower-level
:class:`~smolvm.vm.SmolVMManager` manager, giving callers an instance-style
interface::

    from smolvm import SmolVM

    with SmolVM(config) as vm:
        # VM auto-starts on context entry
        result = vm.run("uname -r")
        print(result.stdout)
"""

from __future__ import annotations

import json
import logging
import platform
import shlex
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlparse

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from smolvm._naming import generate_sandbox_name
from smolvm.callbacks import Callback, CallbackDispatcher, RunContext
from smolvm.comm import RustHttpVsockChannel
from smolvm.comm.base import CommChannel, CommChannelKind
from smolvm.comm.select import ChannelResolution, VsockNotSupportedError, resolve_comm_channel
from smolvm.env import inject_env_vars, read_env_vars, remove_env_vars
from smolvm.env_windows import (
    inject_env_vars as inject_env_vars_windows,
)
from smolvm.env_windows import (
    read_env_vars as read_env_vars_windows,
)
from smolvm.env_windows import (
    remove_env_vars as remove_env_vars_windows,
)
from smolvm.exceptions import (
    CommandExecutionUnavailableError,
    OperationTimeoutError,
    SmolVMError,
)
from smolvm.images.boot import BootImage
from smolvm.images.cloud_init import (
    build_seed_iso,
    default_meta_data,
    default_user_data,
    seed_cache_key,
)
from smolvm.images.manager import ImageManager
from smolvm.kernels import ensure_base_kernel_for_backend
from smolvm.runtime.backends import (
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
    resolve_backend,
)
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    get_boot_profile_spec,
    to_published_arch,
)
from smolvm.ssh import ShellKind, SSHClient
from smolvm.types import (
    BrowserSessionConfig,
    BrowserViewport,
    CommandResult,
    DisplaySandboxProtocol,
    GuestOS,
    InternetSettings,
    PortForwardConfig,
    QemuMachine,
    SnapshotInfo,
    SnapshotType,
    VMConfig,
    VMInfo,
    VMState,
    VsockConfig,
    WorkspaceMount,
)
from smolvm.vm import SmolVMManager

logger = logging.getLogger(__name__)

_DEFAULT_RUN_READY_TIMEOUT = 30.0
# Display sandboxes need more than the VM-only 30s timeout because startup
# includes X11/Wayland-style display services, VNC/noVNC, a window manager, and
# sometimes Chromium plus GPU/driver initialization. 90s was chosen
# empirically; tune it if startup profiling shows a tighter bound is reliable.
_DEFAULT_DISPLAY_SANDBOX_BOOT_TIMEOUT = 90.0

# When auto-selecting the channel, how long to wait for the vsock agent before
# falling back to SSH. Kept short: the agent binds its vsock port early in boot
# (before sshd, ~0.9s guest uptime on Alpine), so if it hasn't answered by now
# the image is likely missing the agent or did not start it and SSH is the real
# path. This is a guardrail: an agent-less image costs ~2.5s of wasted probe.
# Images SmolVM builds ship the standalone Rust agent and should answer inside
# this window.
_VSOCK_AUTO_PROBE_TIMEOUT = 2.5
_LOCAL_FORWARD_PROBE_TIMEOUT = 2.0
_LOCAL_FORWARD_PROBE_INTERVAL = 0.2
_LOCAL_TUNNEL_START_TIMEOUT = 10.0
_QEMU_MACHINE_ADAPTER = TypeAdapter(QemuMachine)
_DisplaySandboxT = TypeVar("_DisplaySandboxT", bound=DisplaySandboxProtocol)
_LOCAL_FORWARD_MAX_PORT_ATTEMPTS = 10
_AUTO_CONFIG_DEFAULT_MEM_SIZE_MIB = {
    GuestOS.ALPINE: 512,
    GuestOS.UBUNTU: 1024,
    GuestOS.WINDOWS: 4096,
}
_AUTO_CONFIG_DEFAULT_DISK_SIZE_MIB = {
    GuestOS.ALPINE: 512,
    GuestOS.UBUNTU: 2048,
    # WINDOWS: not used. The Windows path always supplies a pre-built
    # qcow2 in Phase 1, so we don't size or grow a disk for it.
}


def _vsock_not_supported_message(vm_id: str, error: VsockNotSupportedError) -> str:
    """Format a user-facing explicit-vsock failure at the VM boundary."""
    return (
        f"Cannot use vsock for sandbox '{vm_id}': {error.reason}; "
        "create or reconnect with comm_channel='ssh'."
    )


def _normalize_guest_os(os: GuestOS | str | None) -> GuestOS:
    """Normalize guest OS input for auto-config flows."""
    if os is None:
        return GuestOS.ALPINE
    if isinstance(os, GuestOS):
        return os
    try:
        return GuestOS(os)
    except ValueError as exc:
        valid_values = ", ".join(guest_os.value for guest_os in GuestOS)
        raise ValueError(f"Unsupported guest OS {os!r}. Valid values: {valid_values}.") from exc


def _validate_qemu_machine(qemu_machine: object) -> QemuMachine:
    """Validate a qemu_machine API value using the VMConfig field contract."""
    return _QEMU_MACHINE_ADAPTER.validate_python(qemu_machine)


def _default_guest_os_for_backend(backend: str) -> GuestOS:
    """Return the default guest OS for the selected backend."""
    if backend == BACKEND_QEMU:
        return GuestOS.UBUNTU
    return GuestOS.ALPINE


def _qcow2_virtual_size_mib(qcow2_path: Path) -> int:
    """Return the guest-visible virtual size of a qcow2 image in MiB."""
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(qcow2_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout)
    return int(info["virtual-size"]) // (1024 * 1024)


def _path_size_mib(path: Path) -> int:
    """Return a path size rounded up to MiB."""
    return (path.stat().st_size + (1024 * 1024) - 1) // (1024 * 1024)


def _existing_vm_ids() -> set[str]:
    """Best-effort lookup of existing VM IDs for collision-free auto-naming.

    Returns an empty set if the state store cannot be read (e.g. the data dir
    doesn't exist yet on a fresh install). The downstream ``create`` call still
    enforces uniqueness via the storage layer, so a stale or empty answer
    here can never produce a duplicate VM — at worst it triggers one extra
    retry.
    """
    from smolvm.storage import create_state_manager
    from smolvm.vm import resolve_data_dir

    try:
        db_path = resolve_data_dir() / "smolvm.db"
        if not db_path.exists():
            return set()
        state = create_state_manager(db_path=db_path)
        return {info.vm_id for info in state.list_vms()}
    except Exception as exc:
        logger.debug("Could not enumerate existing VM IDs for auto-naming: %s", exc)
        return set()


def _resolve_vm_name(vm_name: str | None, *, prefix: str = "sbx") -> str:
    """Return the user-supplied name or auto-generate one with *prefix*."""
    if vm_name is not None:
        return vm_name
    return generate_sandbox_name(_existing_vm_ids(), prefix=prefix)


def _resolve_auto_config_public_key(ssh_key_path: str | None) -> tuple[str, Path]:
    """Resolve the SSH private key path and matching public key for auto-config."""
    from smolvm.utils import ensure_ssh_key

    if ssh_key_path is None:
        private_key, public_key = ensure_ssh_key()
        return str(private_key), public_key

    public_key = Path(f"{ssh_key_path}.pub")
    if not public_key.is_file():
        raise ValueError(f"ssh_key_path must have a matching public key file at {public_key}")
    return ssh_key_path, public_key


def _parse_mount_specs(specs: list[str], *, writable: bool = False) -> list[WorkspaceMount]:
    """Parse ``HOST_PATH[:GUEST_PATH]`` strings into WorkspaceMount objects.

    If no guest path is given, the mount defaults to ``/workspace``
    (for a single mount) or ``/workspace-N`` (for multiples).

    When ``writable`` is True, every parsed mount is marked writable so
    guest writes propagate to the host directory.
    """
    mounts: list[WorkspaceMount] = []
    for index, spec in enumerate(specs):
        # Split on the *last* colon to allow Windows-style paths in the future
        if ":" in spec:
            host_str, guest_path = spec.rsplit(":", 1)
        else:
            host_str = spec
            guest_path = "/workspace" if len(specs) == 1 else f"/workspace-{index}"
        mounts.append(
            WorkspaceMount(host_path=Path(host_str), guest_path=guest_path, writable=writable)
        )
    return mounts


def _guest_parent_dir(path: str) -> str:
    """Return the POSIX parent directory for a guest path, if it has one."""
    normalized = path.rstrip("/")
    if not normalized:
        return ""
    if "/" not in normalized:
        return ""
    parent = normalized.rsplit("/", 1)[0]
    return parent or "/"


def _is_windows_guest_path(path: str) -> bool:
    """Return True if *path* looks like a Windows-style guest path.

    Accepts the three forms OpenSSH-Win32 SFTP normalizes:
    ``C:\\...`` (Windows-native), ``C:/...`` (forward-slash mix), and
    ``/C:/...`` (SFTP/cygwin-style POSIX prefix). POSIX paths (``/foo``)
    are still POSIX — only paths with a drive-letter colon qualify.
    """
    if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/") and path[0].isalpha():
        return True
    # /C:/... — leading slash + drive letter, e.g. "/C:/Users/foo".
    return (
        len(path) >= 4
        and path[0] == "/"
        and path[2] == ":"
        and path[3] in ("\\", "/")
        and path[1].isalpha()
    )


def _windows_guest_parent_dir(path: str) -> str:
    """Return the parent directory of a Windows-style guest path.

    Uses :mod:`ntpath` so we handle ``\\``, ``/``, and mixed separators
    the same way Windows itself does.
    """
    import ntpath

    normalized = path.rstrip("\\/")
    if not normalized:
        return ""
    parent = ntpath.dirname(normalized)
    # ntpath.dirname("C:\\foo") -> "C:\\", but ntpath.dirname("C:foo") -> "C:".
    # Either form is fine to pass to PowerShell's New-Item -Path.
    return parent


def _windows_path_for_powershell(path: str) -> str:
    """Convert any accepted Windows path form into one PowerShell will parse.

    Paramiko/SFTP accepts the leading-slash SFTP form (``/C:/Users/foo``),
    the bare drive-letter forward-slash form (``C:/Users/foo``), and the
    Windows-native backslash form (``C:\\Users\\foo``) interchangeably.
    PowerShell's path parser, however, treats a leading ``/`` as a
    drive-relative path from the current PSDrive root and chokes on
    ``/C:/...``. This helper strips the leading slash (if any) and
    normalises separators to backslashes so the result is always a
    native Windows path PowerShell can consume.

    Input shapes handled:
      - ``/C:/Users/foo`` -> ``C:\\Users\\foo``
      - ``C:/Users/foo``  -> ``C:\\Users\\foo``
      - ``C:\\Users\\foo`` -> ``C:\\Users\\foo`` (unchanged)
    """
    stripped = path.lstrip("/")
    return stripped.replace("/", "\\")


def _build_auto_config_image_name(
    guest_os: GuestOS,
    *,
    backend: str,
    disk_size_mib: int,
) -> str:
    """Build an OS-aware cache key for auto-configured images."""
    image_name = f"{guest_os.value}-ssh-key"
    if backend == BACKEND_QEMU:
        arch = platform.machine().lower()
        image_arch = "aarch64" if arch in {"arm64", "aarch64"} else "x86_64"
        image_name = f"{image_name}-{image_arch}"

    default_disk_size_mib = _AUTO_CONFIG_DEFAULT_DISK_SIZE_MIB[guest_os]
    if disk_size_mib != default_disk_size_mib:
        image_name = f"{image_name}-{disk_size_mib}m"
    return image_name


def _is_local_image(image: str) -> bool:
    """Return True when *image* points at a host filesystem path.

    Locals are absolute paths (``/abs/...``), tilde-relative paths
    (``~/...``), bare relative paths, and ``file://`` URIs. Anything
    with a non-file scheme (``s3://``, ``http(s)://``, etc.) is remote.
    """
    parsed = urlparse(image)
    if parsed.scheme == "":
        return True
    return parsed.scheme == "file"


def _build_local_image_config(
    *,
    image: str,
    os_input: GuestOS | str | None,
    backend: str | None,
    memory: int | None,
    ssh_key_path: str | None,
    qemu_machine: QemuMachine = "auto",
    vm_name: str | None = None,
    name_prefix: str = "sbx",
) -> tuple[VMConfig, str | None]:
    """Build a VMConfig from a host filesystem image (path or ``file://``).

    Required when *os_input* is set — the file alone doesn't tell us
    which OS is inside. For Phase 1, only Windows is meaningfully
    supported on this path; Linux users still go through auto-config
    or S3.

    Each sandbox gets a thin per-VM qcow2 overlay stacked on top of the
    user's baseline (``disk_mode="isolated"``), so concurrent
    ``SmolVM(image=...)`` calls don't collide on the write lock and the
    baseline itself stays read-only and uncorrupted. The overlay lives
    under ``data_dir/disks/{vm_id}.qcow2`` and is created near-instantly
    via ``qemu-img create -b``. Power users who need writes to land in
    the baseline (e.g., a one-off image-baking workflow) can construct
    a ``VMConfig`` directly with ``disk_mode="shared"``.
    """
    if os_input is None:
        raise ValueError(
            'Local images need os= (e.g. SmolVM(os="windows", image="..."))'
            " so SmolVM knows which operating system is inside the file."
        )
    guest_os = _normalize_guest_os(os_input)
    if guest_os is not GuestOS.WINDOWS:
        raise ValueError(
            f"Local images currently only support os='windows' (got "
            f"os={guest_os.value!r}); use auto-config or an S3 image for Linux."
        )

    parsed = urlparse(image)
    raw_path = parsed.path if parsed.scheme == "file" else image
    local_path = Path(raw_path).expanduser().resolve()
    if not local_path.is_file():
        raise ValueError(
            f"Local image {local_path} does not exist; pass image= as an "
            "absolute path or file:// URI to an existing qcow2 file."
        )

    if backend is not None:
        try:
            resolved_backend = resolve_backend(backend)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if resolved_backend != BACKEND_QEMU:
            raise ValueError(
                f"Windows guests only run on the QEMU backend; drop "
                f'backend={backend!r} or pass backend="qemu".'
            )

    if ssh_key_path is None:
        from smolvm.utils import ensure_ssh_key

        private_key, _ = ensure_ssh_key()
        ssh_key_path = str(private_key)

    resolved_vm_name = _resolve_vm_name(vm_name, prefix=name_prefix)

    # Memory defaults follow the deep-dive recommendation: 4 GiB for
    # Windows 11 (it runs at 2 GiB minimum but Edge + Defender want more).
    default_mem_mib = _AUTO_CONFIG_DEFAULT_MEM_SIZE_MIB.get(GuestOS.WINDOWS, 4096)

    config = VMConfig(
        vm_id=resolved_vm_name,
        vcpu_count=4,
        memory=memory if memory is not None else default_mem_mib,
        guest_os=GuestOS.WINDOWS,
        kernel_path=None,
        rootfs_path=local_path,
        backend=BACKEND_QEMU,
        qemu_machine=qemu_machine,
        boot_mode="firmware",
        boot_args="",  # ignored in firmware mode
        # Phase 2: the contract for the Windows path is that the user's
        # qcow2 has OpenSSH server set up. Declaring ssh_capable=True
        # lets vm.run() / vm.upload_file() / wait_for_ssh() work; if
        # the user's image doesn't actually have sshd, those calls fail
        # at connect-time with a clear paramiko auth/connection error.
        ssh_capable=True,
        # Per-VM qcow2 overlay on top of the user's baseline. The same
        # _materialize_rootfs path Linux Alpine/Ubuntu use; the baseline
        # stays read-only so multiple Windows sandboxes can run in
        # parallel from the same image without colliding on the write
        # lock, and a crashed sandbox doesn't dirty the original.
        disk_mode="isolated",
    )
    logger.info(
        "Configured Windows VM from local image: %s (image=%s)",
        resolved_vm_name,
        local_path,
    )
    return config, ssh_key_path


def _build_s3_image_config(
    *,
    image: str,
    vm_name: str | None = None,
    name_prefix: str = "sbx",
    backend: str | None = None,
    qemu_machine: QemuMachine = "auto",
    memory: int | None = None,
    ssh_key_path: str | None = None,
    on_download: Callable[[str, int, int | None], None] | None = None,
) -> tuple[VMConfig, str | None]:
    """Build a VMConfig from an S3-hosted image.

    Downloads the manifest and assets via :class:`ImageManager`, resolves
    boot arguments, and returns a ready-to-use config.
    """
    resolved_backend = resolve_backend(backend)
    resolved_ssh_key_path: str | None = ssh_key_path

    image_manager = ImageManager()
    local_image, manifest = image_manager.ensure_s3_image(image, on_download=on_download)

    # Resolve SSH key for auto-config
    if resolved_ssh_key_path is None:
        from smolvm.utils import ensure_ssh_key

        private_key, _ = ensure_ssh_key()
        resolved_ssh_key_path = str(private_key)

    # Resolve boot args: prefer manifest, fall back to backend default.
    # Images with an initrd typically need the initramfs boot profile
    # (e.g., Ubuntu cloud images), while direct-kernel images use MICROVM_DIRECT.
    if manifest.boot_args:
        boot_args = manifest.boot_args
    else:
        if local_image.initrd_path is not None:
            boot_profile = KernelBootProfile.QEMU_DESKTOP_INITRAMFS
        else:
            boot_profile = KernelBootProfile.MICROVM_DIRECT
        boot_args = get_boot_profile_spec(boot_profile).base_boot_args_for_backend(
            resolved_backend,
            platform.machine(),
        )

    # Cloud images (those with an initrd) typically need a cloud-init
    # seed ISO to inject the SSH public key.
    extra_drives: list[Path] = []
    if local_image.initrd_path is not None:
        public_key_path = Path(f"{resolved_ssh_key_path}.pub")
        if public_key_path.is_file():
            public_key_value = public_key_path.read_text().strip()
            seed_key = seed_cache_key(
                ssh_public_key=public_key_value,
                instance_id=f"smolvm-s3-{manifest.name}",
                hostname="smolvm",
            )
            seed_dir = image_manager.cache_dir / "cloud-init-seeds"
            seed_path = seed_dir / f"{seed_key}.iso"
            if not seed_path.exists():
                build_seed_iso(
                    seed_path,
                    user_data=default_user_data(public_key_value),
                    meta_data=default_meta_data(
                        instance_id=f"smolvm-s3-{manifest.name}",
                        hostname="smolvm",
                    ),
                )
            extra_drives.append(seed_path)

    resolved_vm_name = _resolve_vm_name(vm_name, prefix=name_prefix)
    config = VMConfig(
        vm_id=resolved_vm_name,
        vcpu_count=1,
        memory=memory or 512,
        kernel_path=local_image.kernel_path,
        initrd_path=local_image.initrd_path,
        rootfs_path=local_image.rootfs_path,
        extra_drives=extra_drives,
        boot_args=boot_args,
        ssh_capable=True,
        backend=resolved_backend,
        qemu_machine=qemu_machine,
    )
    logger.info(
        "Configured VM from S3 image: %s (image=%s, backend=%s)",
        resolved_vm_name,
        manifest.name,
        resolved_backend,
    )
    return config, resolved_ssh_key_path


def _build_auto_config(
    *,
    vm_name: str | None = None,
    name_prefix: str = "sbx",
    os: GuestOS | str | None = None,
    backend: str | None = None,
    qemu_machine: QemuMachine = "auto",
    memory: int | None = None,
    disk_size_mib: int | None = None,
    ssh_key_path: str | None = None,
    on_download: Callable[[str, int, int | None], None] | None = None,
) -> tuple[VMConfig, str | None]:
    """Build the default SSH-ready VM config used by zero-config flows."""
    resolved_backend = resolve_backend(backend)
    resolved_os = _normalize_guest_os(os or _default_guest_os_for_backend(resolved_backend))
    if resolved_os is GuestOS.WINDOWS:
        # Auto-config builds a Linux SSH-ready VM from a built or downloaded
        # base image; Windows has no equivalent path because we don't ship a
        # base qcow2. The user must supply their own pre-installed image.
        raise ValueError(
            "Windows guests need a pre-installed disk image; pass "
            'image="/path/to/win11.qcow2" (see '
            "docs/deep-dive/windows-guest-qemu.md to build one)."
        )
    resolved_ssh_key_path, public_key_path = _resolve_auto_config_public_key(ssh_key_path)
    public_key_value = public_key_path.read_text().strip()

    resolved_memory = _AUTO_CONFIG_DEFAULT_MEM_SIZE_MIB[resolved_os]
    if memory is not None:
        resolved_memory = memory
    default_disk_size_mib = _AUTO_CONFIG_DEFAULT_DISK_SIZE_MIB[resolved_os]
    resolved_disk_size_mib = default_disk_size_mib if disk_size_mib is None else disk_size_mib
    if resolved_disk_size_mib < 64:
        raise ValueError("disk_size_mib must be >= 64")

    if resolved_os is GuestOS.UBUNTU:
        from smolvm.images.published import Vmm, ensure_published_image

        vmm_by_backend: dict[str, Vmm] = {
            BACKEND_QEMU: "qemu",
            BACKEND_FIRECRACKER: "firecracker",
            BACKEND_LIBKRUN: "libkrun",
        }
        vmm = vmm_by_backend[resolved_backend]
        arch = to_published_arch(platform.machine())
        resolved_vm_name = _resolve_vm_name(vm_name, prefix=name_prefix)
        local_image = ensure_published_image("ubuntu", arch, vmm, "ubuntu")
        base_rootfs_size_mib = _path_size_mib(local_image.rootfs_path)
        required_disk_size_mib = max(default_disk_size_mib, base_rootfs_size_mib)
        if disk_size_mib is None:
            resolved_disk_size_mib = required_disk_size_mib
        elif resolved_disk_size_mib < required_disk_size_mib:
            quoted_vm_name = shlex.quote(resolved_vm_name)
            raise ValueError(
                f"Ubuntu needs at least {required_disk_size_mib} MiB for sandbox "
                f"'{resolved_vm_name}'; "
                f"recreate it with: smolvm sandbox create --name {quoted_vm_name} --os ubuntu "
                f"--backend {resolved_backend} --disk-size {required_disk_size_mib}."
            )
        should_grow_filesystem = (
            disk_size_mib is not None and resolved_disk_size_mib > base_rootfs_size_mib
        )

        kernel_profile = KernelBootProfile.MICROVM_DIRECT
        boot_args = get_boot_profile_spec(kernel_profile).base_boot_args_for_backend(
            resolved_backend,
            platform.machine(),
        )
        config = VMConfig(
            vm_id=resolved_vm_name,
            vcpu_count=1,
            memory=resolved_memory,
            kernel_path=local_image.kernel_path,
            rootfs_path=local_image.rootfs_path,
            rootfs_format="raw-ext4",
            boot_args=boot_args,
            guest_os=GuestOS.UBUNTU,
            ssh_capable=True,
            backend=resolved_backend,
            qemu_machine=qemu_machine,
            ssh_public_key=public_key_value,
            disk_size_mib=resolved_disk_size_mib,
            grow_filesystem=should_grow_filesystem,
        )
        logger.info(
            "Auto-configured VM: %s (os=ubuntu, backend=%s, source=published-bare-ubuntu)",
            resolved_vm_name,
            resolved_backend,
        )
        return config, resolved_ssh_key_path

    kernel_profile = KernelBootProfile.MICROVM_DIRECT
    boot_args = get_boot_profile_spec(kernel_profile).base_boot_args_for_backend(
        resolved_backend,
        platform.machine(),
    )
    from smolvm.images.builder import ImageBuilder

    builder = ImageBuilder()
    image_name = _build_auto_config_image_name(
        resolved_os,
        backend=resolved_backend,
        disk_size_mib=resolved_disk_size_mib,
    )

    # Pick the kernel format the runtime backend requires. Same kernel source,
    # different container: ``elf`` for Firecracker / libkrun, ``image`` for QEMU.
    from smolvm.images.published import BASE_KERNELS, _kernel_format_for_vmm

    kernel_fmt = _kernel_format_for_vmm(resolved_backend)  # type: ignore[arg-type]
    base_kernel_url = BASE_KERNELS[to_published_arch(platform.machine())].url_for(kernel_fmt)

    kernel, rootfs = builder.build_alpine_ssh_key(
        public_key_path,
        name=image_name,
        rootfs_size_mb=resolved_disk_size_mib,
        kernel_profile=kernel_profile,
        kernel_url=base_kernel_url,
    )

    resolved_vm_name = _resolve_vm_name(vm_name, prefix=name_prefix)
    config = VMConfig(
        vm_id=resolved_vm_name,
        vcpu_count=1,
        memory=resolved_memory,
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=boot_args,
        backend=resolved_backend,
        qemu_machine=qemu_machine,
        ssh_public_key=public_key_value,
    )
    logger.info(
        "Auto-configured VM: %s (os=%s, backend=%s)",
        resolved_vm_name,
        resolved_os.value,
        resolved_backend,
    )
    return config, resolved_ssh_key_path


def _from_image_config_help(vm_id: str) -> str:
    """Return the recovery command shown by from_image validation errors."""
    return f"smolvm sandbox create --name {vm_id} --help"


def _from_image_port_help(vm_id: str) -> str:
    """Return the recovery command shown by port-forward validation errors."""
    return f"smolvm sandbox port expose {vm_id} --help"


def _normalize_from_image_arch(image: BootImage, arch: str | None, vm_id: str) -> str:
    """Resolve the architecture used for kernel lookup and boot-arg rendering."""
    try:
        requested_arch = to_published_arch(platform.machine()) if arch in {None, "host"} else arch
        if requested_arch not in {"amd64", "arm64"}:
            requested_arch = to_published_arch(requested_arch)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported image architecture for sandbox '{vm_id}'; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        ) from exc
    if image.arch is not None and arch not in {None, "host"} and image.arch != requested_arch:
        raise ValueError(
            f"Boot image was prepared for arch={image.arch!r}, but arch={requested_arch!r} "
            f"was requested; to fix, run `{_from_image_config_help(vm_id)}`."
        )
    return image.arch or requested_arch


def _normalize_from_image_backend(image: BootImage, backend: str | None, vm_id: str) -> str:
    """Resolve the backend used to launch a BootImage."""
    if image.boot_mode == "firmware" and backend is None and image.backend is None:
        return BACKEND_QEMU

    try:
        resolved_backend = resolve_backend(backend or image.backend)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported backend for sandbox '{vm_id}'; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        ) from exc
    if image.backend is not None and backend is not None and image.backend != resolved_backend:
        raise ValueError(
            f"Boot image was prepared for backend={image.backend!r}, but "
            f"backend={resolved_backend!r} was requested; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        )
    if image.boot_mode == "firmware" and resolved_backend != BACKEND_QEMU:
        raise ValueError(
            f"Firmware boot images require backend='qemu'; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        )
    if image.rootfs_format == "qcow2" and resolved_backend != BACKEND_QEMU:
        raise ValueError(
            f"qcow2 boot images require backend='qemu'; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        )
    if image.initrd_path is not None and resolved_backend == BACKEND_FIRECRACKER:
        raise ValueError(
            f"initrd boot images are not supported with backend='firecracker'; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        )
    return resolved_backend


def _normalize_from_image_network(
    *,
    backend: str,
    network: Literal["tap", "slirp"] | None,
    vm_id: str,
) -> Literal["tap", "slirp"]:
    """Return the QEMU network mode for a BootImage launch."""
    if network is None:
        return "slirp"
    if network not in {"tap", "slirp"}:
        raise ValueError(
            f"network must be 'tap' or 'slirp'; to fix, run `{_from_image_config_help(vm_id)}`."
        )
    if backend != BACKEND_QEMU and network == "slirp":
        raise ValueError(
            f"network='slirp' is only supported with backend='qemu'; to fix, run "
            f"`{_from_image_config_help(vm_id)}`."
        )
    return network


def _normalize_port_forwards(
    forwards: list[PortForwardConfig | dict[str, Any]] | None,
    *,
    vm_id: str,
) -> list[PortForwardConfig]:
    """Normalize from_image port-forward inputs."""
    normalized: list[PortForwardConfig] = []
    for forward in forwards or []:
        if isinstance(forward, PortForwardConfig):
            normalized.append(forward)
        else:
            try:
                normalized.append(PortForwardConfig(**forward))
            except PydanticValidationError as exc:
                raise ValueError(
                    f"Invalid port-forward entry for sandbox '{vm_id}'; to fix, run "
                    f"`{_from_image_port_help(vm_id)}`."
                ) from exc
    return normalized


def _normalize_vsock_config(vsock: VsockConfig | dict[str, Any] | None) -> VsockConfig | None:
    """Normalize from_image vsock input."""
    if vsock is None or isinstance(vsock, VsockConfig):
        return vsock
    return VsockConfig(**vsock)


def _normalize_display_viewport(
    viewport: BrowserViewport | dict[str, Any] | None,
    *,
    width: int,
    height: int,
) -> BrowserViewport:
    """Return a viewport object for browser and desktop sandboxes."""
    if viewport is None:
        return BrowserViewport(
            width=_validate_positive_int("viewport_width", width),
            height=_validate_positive_int("viewport_height", height),
        )
    if isinstance(viewport, BrowserViewport):
        _validate_positive_int("viewport.width", viewport.width)
        _validate_positive_int("viewport.height", viewport.height)
        return viewport
    try:
        raw_width = viewport["width"]
        raw_height = viewport["height"]
    except KeyError as exc:
        raise ValueError("viewport must include positive integer width and height") from exc
    return BrowserViewport(
        width=_validate_positive_int("viewport.width", raw_width),
        height=_validate_positive_int("viewport.height", raw_height),
    )


def _validate_positive_int(name: str, value: Any) -> int:
    """Return a positive integer or raise a parameter-specific ValueError."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_int_range(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    """Return an integer within range or raise a parameter-specific ValueError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _validate_timeout_range(name: str, value: Any, *, maximum: float) -> float:
    """Return a positive timeout or raise a parameter-specific ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number greater than 0 and at most {maximum}.")
    normalized = float(value)
    if normalized <= 0 or normalized > maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}.")
    return normalized


def _validate_display_sandbox_limits(
    *,
    memory_mb: int,
    disk_size_mb: int,
    timeout_minutes: int,
    boot_timeout: float,
) -> float:
    """Validate browser/desktop factory limits before building VM config."""
    _validate_int_range("memory_mb", memory_mb, minimum=512, maximum=16384)
    _validate_int_range("disk_size_mb", disk_size_mb, minimum=2048, maximum=16384)
    _validate_int_range("timeout_minutes", timeout_minutes, minimum=1, maximum=240)
    return _validate_timeout_range("boot_timeout", boot_timeout, maximum=3600.0)


@dataclass(slots=True)
class _LocalForward:
    """Internal tracking for localhost exposure transport."""

    host_port: int
    guest_port: int
    transport: Literal["nftables", "ssh_tunnel"]
    tunnel_proc: subprocess.Popen[str] | None = None


class SmolVM:
    """High-level interface for a single microVM.

    Create a VM with a config, reconnect to an existing one by ID,
    or call ``SmolVM()`` for an auto-configured SSH-ready VM.

    Args:
        config: VM configuration. Mutually exclusive with *vm_id* and *image*.
            If omitted (and *vm_id* is omitted), SmolVM auto-creates
            a default SSH-capable VM configuration.
        vm_id: ID of an existing VM to reconnect to.
        image: Image URI (e.g. ``s3://bucket/images/alpine/``).
            Mutually exclusive with *config*, *vm_id*, and *os*.
            The image is downloaded and cached locally before VM creation.
        data_dir: Override the default data directory.
        socket_dir: Override the default socket directory.
        backend: Runtime backend override (``firecracker``, ``qemu``, or ``auto``).
        os: Guest OS for auto-config mode (``"alpine"`` or ``"ubuntu"``).
        qemu_machine: QEMU machine model (``"auto"``, ``"q35"``, or ``"microvm"``).
        memory: Guest memory in MiB for auto-config mode (``SmolVM()`` only).
        disk_size: Root filesystem size in MiB for auto-config mode (``SmolVM()`` only).
        ssh_user: SSH user for :meth:`run` (default ``root``; pass the
            Windows local-admin username for Windows guests, e.g.
            ``"Administrator"`` or whatever you baked into the qcow2).
        ssh_key_path: Optional SSH private key path. If omitted,
            SmolVM first tries default SSH auth, then falls back to
            ``~/.smolvm/keys/id_ed25519`` when needed.
        ssh_password: Optional SSH password (paramiko password auth).
            Used when the guest has password-only SSH (e.g. a Windows
            POC qcow2). **Takes precedence over** *ssh_key_path*: when
            ``ssh_password`` is set, ``ssh_key_path`` is ignored and
            password auth is used. (paramiko silently prefers
            ``key_filename`` over ``password`` if both are passed —
            SmolVM clears the key path internally so the password is
            actually tried.)
        internet_settings: Network access controls. Accepts an
            :class:`~smolvm.types.InternetSettings` instance or a dict
            (e.g. ``{"allowed_domains": ["https://example.com/"]}``).
            When set, only the listed domains are reachable from the VM.
        mounts: Host directories to mount inside the guest, as
            ``HOST_PATH[:GUEST_PATH]`` strings. Equivalent to passing
            ``WorkspaceMount`` instances on a :class:`VMConfig`.
        writable_mounts: When ``True``, every entry in *mounts* is
            exposed read-write so guest writes propagate to the host
            directory. Default ``False`` keeps the host read-only with
            a writable in-VM overlay (changes stay inside the VM).
            For per-mount control, set
            :attr:`~smolvm.types.WorkspaceMount.writable` directly on
            ``config.workspace_mounts`` instead.

    Raises:
        ValueError: If both *config* and *vm_id* are given, or if auto-config-only
            options are used together with either of them.
    """

    def __init__(
        self,
        config: VMConfig | None = None,
        *,
        vm_id: str | None = None,
        image: str | None = None,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
        os: GuestOS | str | None = None,
        qemu_machine: QemuMachine = "auto",
        memory: int | None = None,
        disk_size: int | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
        comm_channel: CommChannelKind | None = None,
        internet_settings: InternetSettings | dict[str, Any] | None = None,
        mounts: list[str] | None = None,
        writable_mounts: bool = False,
        callbacks: list[Callback] | None = None,
    ) -> None:
        if config is not None and vm_id is not None:
            raise ValueError("Provide either config or vm_id, not both.")

        if comm_channel is not None and comm_channel not in ("ssh", "vsock"):
            raise ValueError(f"comm_channel must be 'ssh' or 'vsock', got {comm_channel!r}")

        if config is not None and qemu_machine != "auto":
            config = config.model_copy(
                update={"qemu_machine": _validate_qemu_machine(qemu_machine)}
            )

        if image is not None and (config is not None or vm_id is not None):
            raise ValueError("image cannot be combined with config or vm_id.")

        # Local images (file:// URIs and absolute / ~ paths) need os= to be
        # set since the file alone doesn't self-identify. S3 images carry
        # their own OS info in the manifest, so os= would conflict.
        if image is not None and os is not None and not _is_local_image(image):
            raise ValueError(
                "image and os are mutually exclusive for S3 images (the "
                "manifest already names the OS); drop one of them."
            )

        if (config is not None or vm_id is not None) and (
            memory is not None or disk_size is not None or os is not None
        ):
            raise ValueError(
                "memory, disk_size, and os only apply in auto-config mode; "
                "drop them or drop config=/vm_id=."
            )

        # Phase 1 Windows guest scope locks. Each is intentionally narrow
        # so misconfigurations fail fast with a plain-English message
        # before we burn time materializing rootfs / firmware.
        if os is not None and _normalize_guest_os(os) is GuestOS.WINDOWS:
            if image is None:
                raise ValueError(
                    "Windows guests need a pre-installed disk image; pass "
                    'image="/path/to/win11.qcow2" (see '
                    "docs/deep-dive/windows-guest-qemu.md to build one)."
                )
            if mounts:
                raise ValueError(
                    "Workspace mounts (mounts=) are not yet supported for "
                    "Windows guests in this release; drop the mounts= arg."
                )
            if internet_settings is not None:
                raise ValueError(
                    "Egress controls (internet_settings=) are not yet "
                    "supported for Windows guests; drop the internet_settings= arg."
                )

        # Workspace mounts currently require the QEMU backend (virtio-9p).
        # When no backend was pinned and mounts were requested — either via
        # the `mounts=` kwarg or via `config.workspace_mounts` on a pre-built
        # VMConfig — default to QEMU. Callers who explicitly set `backend`
        # (on the kwarg or the config) still get the SmolVMManager error if
        # their choice can't host mounts.
        wants_mounts = bool(mounts) or (config is not None and bool(config.workspace_mounts))
        if (
            backend is None
            and wants_mounts
            and vm_id is None
            and (config is None or config.backend is None)
        ):
            backend = BACKEND_QEMU

        if image is not None:
            if _is_local_image(image):
                # Local image mode (file:// or path) — needs os= to
                # disambiguate; the file alone doesn't self-identify.
                logger.info("Resolving local image at %s...", image)
                config, ssh_key_path = _build_local_image_config(
                    image=image,
                    os_input=os,
                    backend=backend,
                    qemu_machine=qemu_machine,
                    memory=memory,
                    ssh_key_path=ssh_key_path,
                )
            else:
                # S3 image mode
                logger.info("Resolving image from %s...", image)
                config, ssh_key_path = _build_s3_image_config(
                    image=image,
                    backend=backend,
                    qemu_machine=qemu_machine,
                    memory=memory,
                    ssh_key_path=ssh_key_path,
                )
        elif config is None and vm_id is None:
            # Auto-configuration mode
            logger.info("No config provided; auto-configuring standard SSH VM...")
            config, ssh_key_path = _build_auto_config(
                os=os,
                backend=backend,
                qemu_machine=qemu_machine,
                memory=memory,
                disk_size_mib=disk_size,
                ssh_key_path=ssh_key_path,
            )

        # Normalize and merge internet_settings into the config
        if internet_settings is not None:
            if vm_id is not None:
                raise ValueError(
                    "internet_settings cannot be set when reconnecting to an existing VM."
                )
            if isinstance(internet_settings, dict):
                internet_settings = InternetSettings(**internet_settings)
            if config is not None and config.internet_settings is not None:
                raise ValueError(
                    "internet_settings is already set on the provided VMConfig; "
                    "pass it in one place only (either on the config or as a keyword argument)."
                )
            if config is not None:
                config = config.model_copy(update={"internet_settings": internet_settings})

        # Normalize and merge mounts into the config
        if mounts is not None:
            if vm_id is not None:
                raise ValueError("mounts cannot be set when reconnecting to an existing VM.")
            workspace_mounts = _parse_mount_specs(mounts, writable=writable_mounts)
            if config is not None:
                if config.workspace_mounts:
                    raise ValueError(
                        "workspace_mounts is already set on the provided VMConfig; "
                        "pass it in one place only."
                    )
                config = config.model_copy(update={"workspace_mounts": workspace_mounts})
        elif writable_mounts:
            raise ValueError(
                "writable_mounts requires the mounts= argument; nothing to "
                "make writable. Alternatively, set "
                "WorkspaceMount(writable=True) on config.workspace_mounts."
            )

        self._ssh_user = ssh_user
        self._ssh_key_path = ssh_key_path
        self._ssh_password = ssh_password
        self._comm_channel_request: CommChannelKind | None = comm_channel
        self._default_ssh_key_path: str | None = None

        sdk_kwargs: dict[str, Any] = {}
        if data_dir is not None:
            sdk_kwargs["data_dir"] = data_dir
        if socket_dir is not None:
            sdk_kwargs["socket_dir"] = socket_dir
        if backend is not None:
            sdk_kwargs["backend"] = backend

        # Record the channel preference on the config so create() can reserve a
        # vsock CID and wire the device before boot. Covers both an explicit
        # config and one built from image=.
        if config is not None and comm_channel is not None:
            config = config.model_copy(update={"comm_channel": comm_channel})

        if config is not None:
            self._sdk = SmolVMManager(**sdk_kwargs)
            self._info = self._sdk.create(config)
            self._vm_id = config.vm_id
            self._owns_vm = True
        else:
            # Reconnect to an existing VM
            assert vm_id is not None
            self._sdk = SmolVMManager.from_id(vm_id, **sdk_kwargs)
            self._info = self._sdk.get(vm_id)
            self._vm_id = vm_id
            self._owns_vm = False

        self._control_channel: CommChannel | None = None
        self._control_ready = False
        self._ssh: SSHClient | None = None
        self._ssh_ready = False
        self._local_forwards: dict[tuple[int, int], _LocalForward] = {}
        self._callbacks = CallbackDispatcher(callbacks)

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def _start_display_sandbox(
        cls,
        sandbox_cls: type[_DisplaySandboxT],
        *,
        mode: Literal["headless", "live", "desktop"],
        session_id: str | None,
        backend: Literal["firecracker", "qemu", "libkrun", "auto"],
        profile_id: str | None,
        persistent: bool,
        timeout_minutes: int,
        viewport: BrowserViewport | dict[str, Any] | None,
        viewport_width: int,
        viewport_height: int,
        record_video: bool,
        allow_downloads: bool,
        env_vars: dict[str, str] | None,
        workspace_mounts: list[WorkspaceMount] | None,
        memory_mb: int,
        disk_size_mb: int,
        data_dir: Path | None,
        socket_dir: Path | None,
        ssh_key_path: str | None,
        boot_timeout: float,
        on_progress: Callable[[str], None] | None,
    ) -> _DisplaySandboxT:
        resolved_boot_timeout = _validate_display_sandbox_limits(
            memory_mb=memory_mb,
            disk_size_mb=disk_size_mb,
            timeout_minutes=timeout_minutes,
            boot_timeout=boot_timeout,
        )
        resolved_viewport = _normalize_display_viewport(
            viewport,
            width=viewport_width,
            height=viewport_height,
        )
        profile_mode: Literal["ephemeral", "persistent"] = (
            "persistent" if persistent or profile_id else "ephemeral"
        )
        config = BrowserSessionConfig(
            session_id=session_id,
            backend=backend,
            mode=mode,
            profile_mode=profile_mode,
            profile_id=profile_id,
            timeout_minutes=timeout_minutes,
            viewport=resolved_viewport,
            viewport_width=resolved_viewport.width,
            viewport_height=resolved_viewport.height,
            record_video=record_video,
            allow_downloads=allow_downloads,
            env_vars=env_vars or {},
            workspace_mounts=workspace_mounts or [],
            mem_size_mib=memory_mb,
            disk_size_mib=disk_size_mb,
        )
        sandbox = sandbox_cls(
            config,
            data_dir=data_dir,
            socket_dir=socket_dir,
            ssh_key_path=ssh_key_path,
        )
        try:
            sandbox.start(boot_timeout=resolved_boot_timeout, on_progress=on_progress)
        except Exception:
            try:
                sandbox.stop()
            except Exception:
                logger.exception("Failed to clean up display sandbox after startup failed.")
            raise
        return sandbox

    @classmethod
    def browser(
        cls,
        *,
        headless: bool = True,
        session_id: str | None = None,
        backend: Literal["firecracker", "qemu", "libkrun", "auto"] = "auto",
        profile_id: str | None = None,
        persistent: bool = False,
        timeout_minutes: int = 30,
        viewport: BrowserViewport | dict[str, Any] | None = None,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        record_video: bool = False,
        allow_downloads: bool = True,
        env_vars: dict[str, str] | None = None,
        workspace_mounts: list[WorkspaceMount] | None = None,
        memory_mb: int = 2048,
        disk_size_mb: int = 4096,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        ssh_key_path: str | None = None,
        boot_timeout: float = _DEFAULT_DISPLAY_SANDBOX_BOOT_TIMEOUT,
        on_progress: Callable[[str], None] | None = None,
    ) -> DisplaySandboxProtocol:
        """Start a browser sandbox and return it once it is ready.

        ``headless=True`` exposes only ``cdp_url`` for browser automation.
        ``headless=False`` also exposes ``viewer_url`` for humans and
        ``display_url`` for VNC-compatible computer-use tools.
        """
        from smolvm.browser import _BrowserSandbox

        return cls._start_display_sandbox(
            _BrowserSandbox,
            mode="headless" if headless else "live",
            session_id=session_id,
            backend=backend,
            profile_id=profile_id,
            persistent=persistent,
            timeout_minutes=timeout_minutes,
            viewport=viewport,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            record_video=record_video,
            allow_downloads=allow_downloads,
            env_vars=env_vars,
            workspace_mounts=workspace_mounts,
            memory_mb=memory_mb,
            disk_size_mb=disk_size_mb,
            data_dir=data_dir,
            socket_dir=socket_dir,
            ssh_key_path=ssh_key_path,
            boot_timeout=boot_timeout,
            on_progress=on_progress,
        )

    @classmethod
    def desktop(
        cls,
        *,
        session_id: str | None = None,
        backend: Literal["firecracker", "qemu", "libkrun", "auto"] = "auto",
        profile_id: str | None = None,
        persistent: bool = False,
        timeout_minutes: int = 30,
        viewport: BrowserViewport | dict[str, Any] | None = None,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        record_video: bool = False,
        allow_downloads: bool = True,
        env_vars: dict[str, str] | None = None,
        workspace_mounts: list[WorkspaceMount] | None = None,
        memory_mb: int = 2048,
        disk_size_mb: int = 4096,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        ssh_key_path: str | None = None,
        boot_timeout: float = _DEFAULT_DISPLAY_SANDBOX_BOOT_TIMEOUT,
        on_progress: Callable[[str], None] | None = None,
    ) -> DisplaySandboxProtocol:
        """Start a visible desktop sandbox and return it once it is ready."""
        from smolvm.browser import _DesktopSandbox

        return cls._start_display_sandbox(
            _DesktopSandbox,
            mode="desktop",
            session_id=session_id,
            backend=backend,
            profile_id=profile_id,
            persistent=persistent,
            timeout_minutes=timeout_minutes,
            viewport=viewport,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            record_video=record_video,
            allow_downloads=allow_downloads,
            env_vars=env_vars,
            workspace_mounts=workspace_mounts,
            memory_mb=memory_mb,
            disk_size_mb=disk_size_mb,
            data_dir=data_dir,
            socket_dir=socket_dir,
            ssh_key_path=ssh_key_path,
            boot_timeout=boot_timeout,
            on_progress=on_progress,
        )

    @classmethod
    def from_id(
        cls,
        vm_id: str,
        *,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
        comm_channel: CommChannelKind | None = None,
    ) -> SmolVM:
        """Reconnect to an existing VM by ID.

        Args:
            vm_id: VM identifier.
            data_dir: Override the default data directory.
            socket_dir: Override the default socket directory.
            backend: Runtime backend override (``firecracker``, ``qemu``, or ``auto``).
            ssh_user: SSH user for :meth:`run`.
            ssh_key_path: Optional SSH private key path. If omitted,
                SmolVM first tries default SSH auth, then falls back to
                ``~/.smolvm/keys/id_ed25519`` when needed.
            ssh_password: Optional SSH password (for Windows guests with
                password-auth qcow2s).

        Returns:
            A :class:`SmolVM` instance bound to the existing VM.

        Raises:
            VMNotFoundError: If no VM with this ID exists.
        """
        return cls(
            vm_id=vm_id,
            data_dir=data_dir,
            socket_dir=socket_dir,
            backend=backend,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            ssh_password=ssh_password,
            comm_channel=comm_channel,
        )

    @classmethod
    def from_image(
        cls,
        image: BootImage,
        *,
        vm_id: str | None = None,
        name_prefix: str = "sbx",
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
        arch: str | None = None,
        qemu_machine: QemuMachine = "auto",
        vcpus: int = 1,
        memory_mb: int = 512,
        guest_os: GuestOS | str = GuestOS.ALPINE,
        network: Literal["tap", "slirp"] | None = None,
        port_forwards: list[PortForwardConfig | dict[str, Any]] | None = None,
        vsock: VsockConfig | dict[str, Any] | None = None,
        comm_channel: CommChannelKind | None = None,
        disk_mode: Literal["isolated", "shared"] = "isolated",
        disk_size_mb: int | None = None,
        grow_filesystem: bool = False,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
        internet_settings: InternetSettings | dict[str, Any] | None = None,
        mounts: list[str] | None = None,
        writable_mounts: bool = False,
        callbacks: list[Callback] | None = None,
    ) -> SmolVM:
        """Create a VM from a custom boot image.

        The image supplies rootfs/kernel boot metadata. This constructor adds
        per-VM runtime settings and delegates disk isolation/network setup to
        the normal SmolVM lifecycle.
        """
        resolved_vm_id = _resolve_vm_name(vm_id, prefix=name_prefix)

        resolved_backend = _normalize_from_image_backend(image, backend, resolved_vm_id)
        resolved_arch = _normalize_from_image_arch(image, arch, resolved_vm_id)
        qemu_network = _normalize_from_image_network(
            backend=resolved_backend,
            network=network,
            vm_id=resolved_vm_id,
        )
        normalized_forwards = _normalize_port_forwards(port_forwards, vm_id=resolved_vm_id)
        if normalized_forwards and (resolved_backend != BACKEND_QEMU or qemu_network != "slirp"):
            raise ValueError(
                f"port_forwards require backend='qemu' and network='slirp'; to fix, run "
                f"`{_from_image_port_help(resolved_vm_id)}`."
            )

        kernel_path = image.kernel_path
        if image.boot_mode == "direct_kernel" and kernel_path is None:
            kernel_path = ensure_base_kernel_for_backend(resolved_backend, arch=resolved_arch)

        config = VMConfig(
            vm_id=resolved_vm_id,
            vcpu_count=vcpus,
            memory=memory_mb,
            guest_os=_normalize_guest_os(guest_os),
            boot_mode=image.boot_mode,
            kernel_path=kernel_path,
            initrd_path=image.initrd_path,
            rootfs_path=image.rootfs_path,
            rootfs_format=image.rootfs_format,
            boot_args=image.render_boot_args(backend=resolved_backend, arch=resolved_arch),
            ssh_capable=image.ssh_capable,
            backend=resolved_backend,
            qemu_network=qemu_network,
            qemu_machine=qemu_machine,
            disk_mode=disk_mode,
            disk_size_mib=disk_size_mb,
            grow_filesystem=grow_filesystem,
            port_forwards=normalized_forwards,
            vsock=_normalize_vsock_config(vsock),
        )
        return cls(
            config=config,
            data_dir=data_dir,
            socket_dir=socket_dir,
            backend=resolved_backend,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            ssh_password=ssh_password,
            comm_channel=comm_channel,
            internet_settings=internet_settings,
            mounts=mounts,
            writable_mounts=writable_mounts,
            callbacks=callbacks,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot_id: str,
        *,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
        resume_vm: bool = False,
        force: bool = False,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        comm_channel: CommChannelKind | None = None,
    ) -> SmolVM:
        """Restore a snapshot and attach a facade to the restored VM."""
        requested_backend = None
        if backend not in (None, "auto"):
            requested_backend = resolve_backend(backend)

        sdk_kwargs: dict[str, Any] = {}
        if data_dir is not None:
            sdk_kwargs["data_dir"] = data_dir
        if socket_dir is not None:
            sdk_kwargs["socket_dir"] = socket_dir
        if backend is not None:
            sdk_kwargs["backend"] = backend

        with SmolVMManager(**sdk_kwargs) as sdk:
            if requested_backend is not None:
                snapshot = sdk.get_snapshot(snapshot_id)
                if snapshot.backend != requested_backend:
                    raise SmolVMError(
                        "Requested backend does not match the snapshot backend",
                        {
                            "snapshot_id": snapshot_id,
                            "requested_backend": requested_backend,
                            "snapshot_backend": snapshot.backend,
                        },
                    )
            vm_info = sdk.restore_snapshot(
                snapshot_id,
                resume_vm=resume_vm,
                force=force,
            )
            restored_backend = resolve_backend(vm_info.config.backend)

        if requested_backend is not None and restored_backend != requested_backend:
            raise SmolVMError(
                "Requested backend does not match the restored VM backend",
                {
                    "snapshot_id": snapshot_id,
                    "requested_backend": requested_backend,
                    "restored_backend": restored_backend,
                },
            )

        return cls(
            vm_id=vm_info.vm_id,
            data_dir=data_dir,
            socket_dir=socket_dir,
            backend=restored_backend,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            comm_channel=comm_channel,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        boot_timeout: float = 30.0,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> SmolVM:
        """Start the VM.

        If the VM config contains ``env_vars``, they are injected into
        the guest via SSH after boot completes.

        Args:
            boot_timeout: Maximum seconds to wait for boot.
            on_progress: Optional callback invoked with a short label
                ("Waiting for SSH...", "Mounting workspace(s)...") at
                each visibly-distinct phase. The CLI uses this to
                update its spinner; programmatic callers can ignore.

        Returns:
            ``self`` for method chaining.

        Raises:
            SmolVMError: If ``env_vars`` is set but the image does not
                support SSH.
        """
        notify = on_progress or (lambda _msg: None)

        if self._info.status == VMState.RUNNING:
            logger.info("VM %s already running; start() is a no-op", self._vm_id)
            return self
        if self._info.status == VMState.PAUSED:
            return self.resume()

        self._info = self._sdk.start(self._vm_id, boot_timeout=boot_timeout)
        self._reset_runtime_state(close_ssh=False)
        logger.info("VM %s started", self._vm_id)

        # Inject environment variables after boot if configured.
        env_vars = self._info.config.env_vars
        if env_vars:
            if not self.can_run_commands():
                raise SmolVMError(
                    "Cannot inject environment variables: VM image does not "
                    "support guest SSH. Use an SSH-capable prebuilt image or "
                    "an image built with ImageBuilder, or bake env vars into "
                    "the rootfs at build time.",
                    {"vm_id": self._vm_id},
                )
            ssh = self._ensure_ssh_for_env(timeout=boot_timeout)
            injector = inject_env_vars_windows if self._is_windows_guest() else inject_env_vars
            injected = injector(ssh, env_vars)
            logger.info(
                "VM %s: injected %d env var(s): %s",
                self._vm_id,
                len(injected),
                ", ".join(injected),
            )

        # Mount workspace directories after boot if configured.
        workspace_mounts = self._info.config.workspace_mounts
        if workspace_mounts:
            if not self.can_run_commands():
                raise SmolVMError(
                    "Cannot mount workspaces: VM image does not support SSH.",
                    {"vm_id": self._vm_id},
                )
            if not self._ssh_ready:
                if on_progress is not None:
                    on_progress("Waiting for SSH...")
                self._wait_for_ssh_over_network(timeout=boot_timeout)
            count = len(workspace_mounts)
            notify("Mounting workspace..." if count == 1 else f"Mounting {count} workspaces...")
            self._mount_workspaces()

        return self

    def stop(self, timeout: float = 3.0) -> SmolVM:
        """Stop the VM.

        Args:
            timeout: Seconds to wait for graceful shutdown.

        Returns:
            ``self`` for method chaining.
        """
        self._cleanup_local_forwards()
        self._info = self._sdk.stop(self._vm_id, timeout=timeout)
        self._reset_runtime_state()
        logger.info("VM %s stopped", self._vm_id)
        return self

    def pause(self) -> SmolVM:
        """Pause the VM."""
        self._cleanup_local_forwards()
        self._info = self._sdk.pause(self._vm_id)
        self._reset_runtime_state()
        logger.info("VM %s paused", self._vm_id)
        return self

    def resume(self) -> SmolVM:
        """Resume the VM."""
        self._info = self._sdk.resume(self._vm_id)
        self._reset_runtime_state(close_ssh=False)
        logger.info("VM %s resumed", self._vm_id)
        return self

    def snapshot(
        self,
        snapshot_id: str | None = None,
        *,
        snapshot_type: SnapshotType | str = SnapshotType.FULL,
        resume_source: bool = False,
    ) -> SnapshotInfo:
        """Create a snapshot for the VM.

        Args:
            snapshot_id: Optional custom snapshot name.
            snapshot_type: ``"full"`` (default) for a self-contained copy that
                always restores on its own, ``"diff"`` to store only what
                changed since the base image to save space, or ``"disk"`` for a
                self-contained disk-only copy that skips the guest RAM dump
                (much faster/lighter; restores as a fresh boot, not a resume).
            resume_source: Keep this VM running after the snapshot is taken.
        """
        try:
            resolved_snapshot_type = SnapshotType(snapshot_type)
        except ValueError as exc:
            allowed = ", ".join(repr(t.value) for t in SnapshotType)
            raise ValueError(
                f"snapshot_type must be one of {allowed}; got {snapshot_type!r}"
            ) from exc
        if resolved_snapshot_type == SnapshotType.DISK:
            self._sync_guest_for_disk_snapshot()
        snapshot_info = self._sdk.create_snapshot(
            self._vm_id,
            snapshot_id=snapshot_id,
            snapshot_type=resolved_snapshot_type,
            resume_source=resume_source,
        )
        self._refresh_info()
        self._reset_runtime_state()
        return snapshot_info

    def _sync_guest_for_disk_snapshot(self) -> None:
        """Flush guest filesystem buffers before copying a disk-only snapshot."""
        self._refresh_info()
        if self._info.status != VMState.RUNNING:
            return
        channel = self._ensure_control_for_operation(action="create a disk snapshot", timeout=10.0)
        result = channel.run("sync", timeout=10, shell="raw")
        if result.exit_code != 0:
            raise SmolVMError(
                f"Cannot create disk snapshot for sandbox '{self._vm_id}' because syncing "
                f"files inside it failed; retry with 'smolvm sandbox snapshot create "
                f"{self._vm_id} --snapshot-type disk'."
            )

    def delete(self) -> None:
        """Delete the VM and release all resources."""
        self._cleanup_local_forwards()
        self._sdk.delete(self._vm_id)
        self._reset_runtime_state()
        logger.info("VM %s deleted", self._vm_id)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run(
        self,
        command: str,
        timeout: int = 30,
        shell: Literal["login", "raw"] = "login",
    ) -> CommandResult:
        """Execute a command on the guest via SSH.

        Lazily creates an :class:`~smolvm.ssh.SSHClient` on first call
        and reuses it for subsequent invocations.

        Args:
            command: Shell command to execute.
            timeout: Maximum seconds to wait for the command.
            shell: Command execution mode:
                - ``"login"`` (default): run via guest login shell.
                - ``"raw"``: execute command directly with no shell wrapping.

        Returns:
            :class:`~smolvm.types.CommandResult`.

        Raises:
            SmolVMError: If the VM is not running or has no network.
            CommandBlockedError: If an ``on_pre_run`` callback vetoes the
                command (or any other exception a pre-run callback raises).
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Start sandbox '{self._vm_id}' before running commands by running "
                f"'smolvm sandbox start {self._vm_id}' "
                f"(current state: {self._info.status.value}).",
                {"vm_id": self._vm_id},
            )
        resolution = self._resolve_channel()
        if not self.can_run_commands():
            raise CommandExecutionUnavailableError(
                vm_id=self._vm_id,
                reason=(
                    "VM config does not advertise an SSH-capable boot path, "
                    "so guest SSH is not guaranteed to start."
                ),
                remediation=self._command_exec_remediation(),
            )
        if resolution.kind == "ssh" and self._info.network is None:
            raise SmolVMError(
                f"Cannot run command in sandbox '{self._vm_id}' over SSH because it has "
                f"no network; remove it with 'smolvm sandbox delete {self._vm_id}' "
                "and create it again.",
                {"vm_id": self._vm_id},
            )

        self._ensure_control_cache_attrs()
        if not self._control_ready:
            try:
                self._wait_for_ready(timeout=_DEFAULT_RUN_READY_TIMEOUT)
            except OperationTimeoutError as e:
                reason = "The guest control channel did not become ready."
                with suppress(Exception):
                    if self._resolve_channel().kind == "ssh":
                        reason = "SSH did not become ready on the guest."
                raise CommandExecutionUnavailableError(
                    vm_id=self._vm_id,
                    reason=reason,
                    remediation=self._command_exec_remediation(),
                ) from e

        if self._control_channel is None:
            raise SmolVMError(
                "Cannot run command: control channel is not initialized",
                {"vm_id": self._vm_id},
            )

        ctx = RunContext(
            vm_id=self._vm_id,
            command=command,
            shell=shell,
            timeout=timeout,
        )
        # Pre-run hooks may veto: any exception they raise (e.g.
        # CommandBlockedError) propagates and the command never runs.
        self._callbacks.fire("on_pre_run", ctx, propagate=True)

        try:
            result = self._control_channel.run(command, timeout=timeout, shell=shell)
        except Exception as exc:
            ctx.error = exc
            self._callbacks.fire("on_run_error", ctx, propagate=False)
            raise

        ctx.result = result
        self._callbacks.fire("on_post_run", ctx, propagate=False)
        return result

    def add_callback(self, callback: Callback) -> SmolVM:
        """Register a :class:`~smolvm.callbacks.Callback` on this VM.

        Returns ``self`` so calls can be chained.
        """
        self._callbacks.add(callback)
        return self

    def _is_windows_guest(self) -> bool:
        """Return True when the running guest is Windows.

        Single source of truth for the per-OS env-management dispatch
        (set/unset/list_env_vars) and for ``start()``'s post-boot
        env injection. Linux guests get the POSIX
        ``/etc/profile.d/smolvm_env.sh`` path; Windows guests get the
        ``HKCU\\Environment`` registry path via
        :mod:`smolvm.env_windows`.
        """
        return self._info.config.guest_os is GuestOS.WINDOWS

    def set_env_vars(self, env_vars: dict[str, str], *, merge: bool = True) -> list[str]:
        """Set environment variables on a running VM.

        On Linux guests the variables are persisted in
        ``/etc/profile.d/smolvm_env.sh`` and affect new login shells.
        On Windows guests they go into ``HKCU\\Environment`` via
        ``[Environment]::SetEnvironmentVariable`` and are visible to
        every fresh process spawned afterwards (subsequent ``vm.run()``
        calls open new SSH sessions, so they see the new values).

        Args:
            env_vars: Key/value pairs to set.
            merge: If True (default), merge with existing variables.

        Returns:
            Sorted variable names present after update.
        """
        if not env_vars:
            return []

        ssh = self._ensure_ssh_for_env()
        if self._is_windows_guest():
            return inject_env_vars_windows(ssh, env_vars, merge=merge)
        return inject_env_vars(ssh, env_vars, merge=merge)

    def unset_env_vars(self, keys: list[str]) -> dict[str, str]:
        """Remove environment variables from a running VM.

        Args:
            keys: Variable names to remove.

        Returns:
            Mapping of removed keys to their previous values.
        """
        if not keys:
            return {}

        ssh = self._ensure_ssh_for_env()
        if self._is_windows_guest():
            return remove_env_vars_windows(ssh, keys)
        return remove_env_vars(ssh, keys)

    def list_env_vars(self) -> dict[str, str]:
        """Return SmolVM-managed environment variables for a running VM."""
        ssh = self._ensure_ssh_for_env()
        if self._is_windows_guest():
            return read_env_vars_windows(ssh)
        return read_env_vars(ssh)

    def upload_file(
        self,
        local_path: str | Path,
        guest_path: str,
        *,
        make_dirs: bool = True,
    ) -> str:
        """Upload one local file into a running VM.

        Overwrites the destination if it already exists.

        Args:
            local_path: File on the host machine.
            guest_path: Absolute POSIX path inside the guest. If it ends
                with ``/``, the local filename is appended.
            make_dirs: Create the destination parent directory first.

        Returns:
            The resolved guest destination path.

        Raises:
            ValueError: If *local_path* is missing, is not a file, or if
                *guest_path* is empty or not an absolute POSIX path.
            SmolVMError: If the VM is not running or file transfer fails.
        """
        source = Path(local_path).expanduser()
        if not source.exists():
            raise ValueError(f"Local file not found: {source}. Check the path and try again.")
        if not source.is_file():
            raise ValueError(
                f"Not a file: {source}. Pass a path to a single file, not a directory."
            )
        if not guest_path:
            raise ValueError("Destination path in the sandbox cannot be empty.")
        is_windows_path = _is_windows_guest_path(guest_path)
        if not guest_path.startswith("/") and not is_windows_path:
            raise ValueError(
                f"Destination path in the sandbox must be absolute "
                f"(start with '/', or a Windows drive-letter path like "
                f"'C:\\\\Users\\\\foo'): {guest_path!r}."
            )

        destination = guest_path
        # Strip a trailing slash/backslash and append the local filename when
        # the destination names a directory. Path style (POSIX vs Windows)
        # determines which separator to add back.
        if destination.endswith("/") or destination.endswith("\\"):
            sep = "\\" if destination.endswith("\\") else "/"
            destination = f"{destination.rstrip(chr(92) + '/')}{sep}{source.name}"

        channel = self._ensure_control_for_file_transfer()
        if make_dirs:
            if is_windows_path:
                parent = _windows_guest_parent_dir(destination)
                if parent:
                    # PowerShell's New-Item -Force creates intermediate
                    # directories and succeeds silently if the path already
                    # exists. Normalise to a native Windows path because
                    # PowerShell's path parser rejects SFTP-style leading
                    # slashes (``/C:/foo`` -> ``C:\\foo``). Single-quoted
                    # Path lets PowerShell treat the backslashes literally
                    # — no escape gymnastics.
                    ps_parent = _windows_path_for_powershell(parent)
                    safe_parent = ps_parent.replace("'", "''")
                    cmd = f"New-Item -ItemType Directory -Force -Path '{safe_parent}' | Out-Null"
                    result = channel.run(cmd, timeout=30, shell="login")
                    if result.exit_code != 0:
                        stderr = result.stderr.strip()
                        raise SmolVMError(
                            f"Could not create directory {parent!r} in the sandbox: {stderr}",
                            {"vm_id": self._vm_id, "guest_path": destination},
                        )
            else:
                parent = _guest_parent_dir(destination)
                if parent:
                    result = channel.run(
                        f"mkdir -p -- {shlex.quote(parent)}",
                        timeout=30,
                        shell="raw",
                    )
                    if result.exit_code != 0:
                        stderr = result.stderr.strip()
                        raise SmolVMError(
                            f"Could not create directory {parent!r} in the sandbox: {stderr}",
                            {"vm_id": self._vm_id, "guest_path": destination},
                        )
        channel.put_file(source, destination)
        return destination

    def download_file(
        self,
        guest_path: str,
        local_path: str | Path,
        *,
        make_dirs: bool = True,
    ) -> str:
        """Download one file from a running VM to the host machine.

        Overwrites the destination if it already exists.

        Args:
            guest_path: Absolute POSIX path of the file inside the guest.
            local_path: Destination path on the host machine. If it ends
                with ``/`` or names an existing directory, the guest
                filename is appended.
            make_dirs: Create the destination parent directory on the
                host if it is missing.

        Returns:
            The resolved local destination path.

        Raises:
            ValueError: If *guest_path* is empty or not an absolute
                POSIX path.
            SmolVMError: If the VM is not running, the local parent
                directory is missing while *make_dirs* is False, or the
                file transfer fails.
        """
        if not guest_path:
            raise ValueError("Source path in the sandbox cannot be empty.")
        if not guest_path.startswith("/") and not _is_windows_guest_path(guest_path):
            raise ValueError(
                f"Source path in the sandbox must be absolute "
                f"(start with '/', or a Windows drive-letter path like "
                f"'C:\\\\Users\\\\foo'): {guest_path!r}."
            )

        raw_local = str(local_path)
        destination = Path(local_path).expanduser()
        treat_as_dir = raw_local.endswith("/") or destination.is_dir()
        if treat_as_dir:
            # Strip trailing separator (either kind) then take the basename.
            guest_name = guest_path.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            if not guest_name:
                raise ValueError(f"Cannot derive a filename from the sandbox path: {guest_path!r}.")
            destination = destination / guest_name

        parent = destination.parent
        if make_dirs:
            parent.mkdir(parents=True, exist_ok=True)
        elif not parent.exists():
            raise SmolVMError(
                f"Local destination directory does not exist: {parent}. "
                f"Create it, or omit --no-create-dirs to create it automatically.",
                {"vm_id": self._vm_id, "local_path": str(destination)},
            )

        channel = self._ensure_control_for_file_transfer()
        channel.get_file(guest_path, destination)
        return str(destination)

    def wait_for_ready(
        self,
        timeout: float = 60.0,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> SmolVM:
        """Wait for the resolved guest control channel to become available.

        Args:
            timeout: Maximum seconds to wait.
            on_progress: Optional callback invoked with the phase label
                ``"Waiting for sandbox..."`` when an actual wait is needed.
                Skipped when the control channel is already ready, so the CLI doesn't
                flash a misleading label for a no-op call.

        Returns:
            ``self`` for method chaining.

        Raises:
            OperationTimeoutError: If SSH is not available in time.
            SmolVMError: If the VM is not running.
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Start sandbox '{self._vm_id}' before waiting for it by running "
                f"'smolvm sandbox start {self._vm_id}' "
                f"(current state: {self._info.status.value}).",
                {"vm_id": self._vm_id},
            )
        if self._resolve_channel().kind == "ssh" and self._info.network is None:
            raise SmolVMError(
                f"Cannot wait for sandbox '{self._vm_id}' over SSH because it has no network; "
                f"remove it with 'smolvm sandbox delete {self._vm_id}' and create it again.",
                {"vm_id": self._vm_id},
            )

        self._ensure_control_cache_attrs()
        if on_progress is not None and not self._control_ready:
            on_progress("Waiting for sandbox...")
        self._wait_for_ready(timeout=timeout)
        return self

    def wait_for_ssh(
        self,
        timeout: float = 60.0,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> SmolVM:
        """Wait for SSH to become available on the guest.

        Use :meth:`wait_for_ready` when you only need the resolved command
        control channel, which may be vsock.
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                "VM is not running. Start the VM using vm.start() before "
                f"waiting for SSH (current state: {self._info.status.value})",
                {"vm_id": self._vm_id},
            )
        if self._info.network is None:
            raise SmolVMError(
                "Cannot wait for SSH: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        if on_progress is not None and not self._ssh_ready:
            on_progress("Waiting for SSH...")
        self._wait_for_ssh_over_network(timeout=timeout)
        return self

    def ssh_commands(
        self,
        *,
        ssh_user: str | None = None,
        key_path: str | Path | None = None,
        public_host: str | None = None,
    ) -> dict[str, str]:
        """Get ready-to-run SSH commands for this VM."""
        return self._sdk.get_ssh_commands(
            self._vm_id,
            ssh_user=ssh_user or self._ssh_user,
            key_path=key_path or self._ssh_key_path,
            public_host=public_host,
        )

    def _ssh_attach_command(self) -> list[str]:
        """Return an interactive SSH command using the resolved ready endpoint."""
        if not self._ssh_ready or self._ssh is None:
            raise SmolVMError(
                "Cannot build interactive SSH command: SSH client is not initialized",
                {"vm_id": self._vm_id},
            )

        command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            str(self._ssh.port),
        ]
        if self._ssh.key_path:
            command.extend(["-i", self._ssh.key_path, "-o", "IdentitiesOnly=yes"])
        command.append(f"{self._ssh.user}@{self._ssh.host}")
        return command

    def _ssh_direct_command(self) -> list[str]:
        """Build an SSH command from VM metadata without probing.

        Uses the VM's known network info and key path to construct the
        command directly, skipping the ``wait_for_ssh`` polling machinery.
        Suitable for interactive CLI use where OpenSSH handles retries.
        """
        self._refresh_info()

        if self._info.network is None:
            raise SmolVMError(
                "Cannot build SSH command: VM has no network configuration",
                {"vm_id": self._vm_id},
            )
        self._sdk.ensure_network_connectivity(self._info)

        # Reuse shared endpoint selection (prefers localhost forward, falls
        # back to guest IP).
        host, port = self._ssh_endpoints()[0]

        # Resolve key: explicit > default SmolVM key.
        key_path = self._ssh_key_path
        explicit_key = key_path is not None
        if key_path is None:
            from smolvm.utils import ensure_ssh_key

            try:
                default_key, _ = ensure_ssh_key()
                key_path = str(default_key)
            except Exception:
                logger.debug(
                    "VM %s: could not resolve default SSH key, falling back to agent/default auth",
                    self._vm_id,
                )

        command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            str(port),
        ]
        if key_path:
            command.extend(["-i", key_path])
            # Only lock to this key when the user explicitly provided it;
            # for the auto-resolved SmolVM key, allow agent/default auth
            # as a fallback.
            if explicit_key:
                command.extend(["-o", "IdentitiesOnly=yes"])
        command.append(f"{self._ssh_user}@{host}")
        return command

    def expose_local(self, guest_port: int, host_port: int | None = None) -> int:
        """Expose a guest TCP port on localhost only.

        Forwards ``127.0.0.1:<host_port>`` on the host to
        ``<guest_ip>:<guest_port>`` inside the VM.

        Args:
            guest_port: Guest TCP port to expose.
            host_port: Host localhost port. If omitted, an available port is chosen.

        Returns:
            The host localhost port to connect to.
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Cannot expose port: VM is {self._info.status.value}",
                {"vm_id": self._vm_id},
            )
        if self._info.network is None:
            raise SmolVMError(
                "Cannot expose port: VM has no network configuration",
                {"vm_id": self._vm_id},
            )
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        self._sdk.ensure_network_connectivity(self._info)

        requested_port = host_port
        if host_port is None:
            host_port = self._allocate_local_port()
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")

        initial_key = (host_port, guest_port)
        existing = self._local_forwards.get(initial_key)
        if existing is not None:
            return existing.host_port

        candidate_ports = [host_port]
        fallback_port = self._allocate_local_port({host_port})
        if fallback_port != host_port:
            candidate_ports.append(fallback_port)

        guest_ip = self._info.network.guest_ip
        attempts: list[str] = []
        should_try_nftables = self._should_try_nftables_local_forward()

        for candidate in candidate_ports:
            key = (candidate, guest_port)
            existing = self._local_forwards.get(key)
            if existing is not None:
                return existing.host_port

            if any(forward.host_port == candidate for forward in self._local_forwards.values()):
                attempts.append(f"localhost:{candidate} already exposed by this VM instance")
                continue

            nftables_configured = False
            keep_nftables = False
            if should_try_nftables:
                try:
                    self._sdk.network.setup_local_port_forward(
                        vm_id=self._vm_id,
                        guest_ip=guest_ip,
                        host_port=candidate,
                        guest_port=guest_port,
                    )
                    nftables_configured = True
                    if self._probe_local_forward(candidate):
                        self._local_forwards[key] = _LocalForward(
                            host_port=candidate,
                            guest_port=guest_port,
                            transport="nftables",
                        )
                        keep_nftables = True
                        logger.info(
                            "VM %s exposed localhost:%d -> guest:%d (transport=nftables)",
                            self._vm_id,
                            candidate,
                            guest_port,
                        )
                        return candidate
                    attempts.append(
                        f"nftables forward localhost:{candidate} -> guest:{guest_port} "
                        "was configured but not reachable"
                    )
                except Exception as e:
                    attempts.append(
                        f"nftables forward localhost:{candidate} -> guest:{guest_port} failed: {e}"
                    )
                finally:
                    if nftables_configured and not keep_nftables:
                        with suppress(Exception):
                            self._sdk.network.cleanup_local_port_forward(
                                vm_id=self._vm_id,
                                guest_ip=guest_ip,
                                host_port=candidate,
                                guest_port=guest_port,
                            )

            try:
                tunnel_proc = self._start_local_tunnel(
                    host_port=candidate,
                    guest_port=guest_port,
                )
                self._local_forwards[key] = _LocalForward(
                    host_port=candidate,
                    guest_port=guest_port,
                    transport="ssh_tunnel",
                    tunnel_proc=tunnel_proc,
                )
                logger.info(
                    "VM %s exposed localhost:%d -> guest:%d (transport=ssh_tunnel)",
                    self._vm_id,
                    candidate,
                    guest_port,
                )
                return candidate
            except Exception as e:
                attempts.append(
                    f"ssh tunnel localhost:{candidate} -> guest:{guest_port} failed: {e}"
                )
                continue

        context = {
            "vm_id": self._vm_id,
            "guest_port": guest_port,
            "requested_host_port": requested_port,
            "candidate_ports": candidate_ports,
            "attempts": attempts,
        }
        details = "; ".join(attempts) if attempts else "no attempts executed"
        raise SmolVMError(
            f"Failed to expose guest port {guest_port} on localhost. {details}",
            context,
        )

    def unexpose_local(self, host_port: int, guest_port: int) -> SmolVM:
        """Remove a previously configured localhost-only port forward."""
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        key = (host_port, guest_port)
        tracked = self._local_forwards.pop(key, None)
        if tracked is not None:
            self._cleanup_local_forward(tracked)
            return self

        self._refresh_info()
        if self._info.network is None:
            raise SmolVMError(
                "Cannot remove local port forward: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        self._sdk.network.cleanup_local_port_forward(
            vm_id=self._vm_id,
            guest_ip=self._info.network.guest_ip,
            host_port=host_port,
            guest_port=guest_port,
        )
        return self

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def vm_id(self) -> str:
        """The VM identifier."""
        return self._vm_id

    @property
    def info(self) -> VMInfo:
        """Current VM runtime information (cached; call :meth:`refresh` to update)."""
        return self._info

    @property
    def status(self) -> VMState:
        """Current VM lifecycle state (cached)."""
        return self._info.status

    @property
    def data_dir(self) -> Path:
        """Directory backing the VM state DB and logs."""
        return self._sdk.data_dir

    def get_ip(self) -> str:
        """Return the guest IP address.

        Raises:
            SmolVMError: If the VM has no network configuration.
        """
        self._refresh_info()
        if self._info.network is None:
            raise SmolVMError(
                "VM has no network configuration",
                {"vm_id": self._vm_id},
            )
        return self._info.network.guest_ip

    def refresh(self) -> SmolVM:
        """Refresh cached VM info from the state store.

        Returns:
            ``self`` for method chaining.
        """
        self._refresh_info()
        return self

    def can_run_commands(self) -> bool:
        """Whether this VM config supports command execution.

        Explicit or auto-selected vsock is command-capable because it uses the
        guest agent. SSH command execution requires a boot path that is
        expected to bring up SSH inside the guest. Three SSH shapes qualify:

        1. ``ssh_capable=True`` — the caller has explicitly declared the
           boot path brings up SSH (used by prebuilt cloud images, S3
           images, and firmware-boot VMs).
        2. A microvm direct-kernel boot with ``init=/init`` in the kernel
           command line (the alpine docker-build path).
        3. A legacy initrd-backed boot with ``ssh_capable`` implicitly set.
        """
        if self._resolve_channel().kind == "vsock":
            return True
        config = self._info.config
        ssh_capable = getattr(config, "ssh_capable", False)
        if ssh_capable is True:
            return True
        return "init=/init" in config.boot_args

    def close(self) -> None:
        """Release underlying SDK resources for this facade instance."""
        self._sdk.close()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    # ==================================================================
    # Async lifecycle methods
    # ==================================================================

    async def async_start(self, boot_timeout: float = 30.0) -> SmolVM:
        """Async version of :meth:`start`."""
        import asyncio

        if self._info.status == VMState.RUNNING:
            return self
        if self._info.status == VMState.PAUSED:
            return self.resume()

        self._info = await self._sdk.async_start(self._vm_id, boot_timeout=boot_timeout)
        self._reset_runtime_state(close_ssh=False)

        env_vars = self._info.config.env_vars
        if env_vars:
            if not self.can_run_commands():
                raise SmolVMError(
                    "Cannot inject environment variables: VM image does not support SSH.",
                    {"vm_id": self._vm_id},
                )
            ssh = await asyncio.to_thread(self._ensure_ssh_for_env, timeout=boot_timeout)
            injector = inject_env_vars_windows if self._is_windows_guest() else inject_env_vars
            await asyncio.to_thread(injector, ssh, env_vars)

        # Mount workspace directories after boot if configured.
        if self._info.config.workspace_mounts:
            if not self.can_run_commands():
                raise SmolVMError(
                    "Cannot mount workspaces: VM image does not support SSH.",
                    {"vm_id": self._vm_id},
                )
            if not self._ssh_ready:
                await asyncio.to_thread(self._wait_for_ssh_over_network, timeout=boot_timeout)
            await asyncio.to_thread(self._mount_workspaces)

        return self

    async def async_stop(self, timeout: float = 3.0) -> SmolVM:
        """Async version of :meth:`stop`."""
        self._cleanup_local_forwards()
        self._info = await self._sdk.async_stop(self._vm_id, timeout=timeout)
        self._reset_runtime_state()
        return self

    async def async_delete(self) -> None:
        """Async version of :meth:`delete`."""
        self._cleanup_local_forwards()
        await self._sdk.async_delete(self._vm_id)
        self._reset_runtime_state()

    async def async_run(
        self,
        command: str,
        timeout: int = 30,
        shell: Literal["login", "raw"] = "login",
    ) -> CommandResult:
        """Async version of :meth:`run`.

        Paramiko is synchronous, so the SSH call is wrapped in
        ``asyncio.to_thread``.
        """
        import asyncio

        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Cannot run command: VM is {self._info.status.value}",
                {"vm_id": self._vm_id},
            )
        if not self.can_run_commands():
            raise CommandExecutionUnavailableError(
                vm_id=self._vm_id,
                reason="VM config does not advertise a command-capable boot path.",
                remediation=self._command_exec_remediation(),
            )

        # Resolve and connect the channel (vsock or SSH) off the event loop,
        # then run the command. Both transports are synchronous.
        channel = await asyncio.to_thread(
            self._ensure_control_for_operation, action="run a command"
        )
        return await asyncio.to_thread(channel.run, command, timeout=timeout, shell=shell)

    async def async_wait_for_ready(self, timeout: float = 60.0) -> SmolVM:
        """Async version of :meth:`wait_for_ready`."""
        import asyncio

        await asyncio.to_thread(self.wait_for_ready, timeout=timeout)
        return self

    async def async_wait_for_ssh(self, timeout: float = 60.0) -> SmolVM:
        """Async version of :meth:`wait_for_ssh`."""
        import asyncio

        await asyncio.to_thread(self.wait_for_ssh, timeout=timeout)
        return self

    @classmethod
    async def async_create_many(
        cls,
        configs: list[VMConfig],
        *,
        boot_timeout: float = 60.0,
        concurrency: int | None = None,
        **kwargs: Any,
    ) -> list[SmolVM]:
        """Create and start multiple VMs concurrently.

        Args:
            configs: List of VM configurations.
            boot_timeout: Maximum seconds to wait for each VM boot.
            concurrency: Maximum parallel VM starts (default: all).
            **kwargs: Passed to SmolVM constructor.

        Returns:
            List of started SmolVM instances.
        """
        import asyncio

        sem = asyncio.Semaphore(concurrency or len(configs))

        async def create_one(config: VMConfig) -> SmolVM:
            async with sem:
                vm = cls(config, **kwargs)
                await vm.async_start(boot_timeout=boot_timeout)
                return vm

        return list(await asyncio.gather(*[create_one(c) for c in configs]))

    async def __aenter__(self) -> SmolVM:
        """Async context manager entry — auto-starts owned VMs."""
        if self._owns_vm and self._info.status != VMState.RUNNING:
            await self.async_start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit — stops and deletes owned VMs."""
        try:
            if self._info.status == VMState.RUNNING:
                try:
                    await self.async_stop()
                except Exception:
                    logger.warning("Failed to stop VM %s on async context exit", self._vm_id)

            if self._owns_vm:
                try:
                    await self.async_delete()
                except Exception:
                    logger.warning("Failed to delete VM %s on async context exit", self._vm_id)
        finally:
            self.close()

    def __enter__(self) -> SmolVM:
        # Auto-start VMs created by this facade instance so callers can
        # immediately interact with the guest inside a context block.
        if self._owns_vm and self._info.status != VMState.RUNNING:
            self.start()
        return self

    def __exit__(self, *args: object) -> None:
        try:
            # Best-effort stop on context exit.
            if self._info.status == VMState.RUNNING:
                try:
                    self.stop()
                except Exception:
                    logger.warning("Failed to stop VM %s on context exit", self._vm_id)

            # Auto-delete only for VMs created by this facade instance.
            if self._owns_vm:
                try:
                    self.delete()
                except Exception:
                    logger.warning("Failed to delete VM %s on context exit", self._vm_id)
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _mount_workspaces(self) -> None:
        """Mount 9p workspace shares with overlayfs inside the guest."""
        assert self._ssh is not None  # noqa: S101 — caller guarantees SSH ready

        workspace_mounts = self._info.config.workspace_mounts
        if not workspace_mounts:
            return

        if self._ssh_user != "root":
            raise SmolVMError(
                "Workspace mounts require ssh_user='root' because the guest "
                "must run modprobe and mount.",
                {"vm_id": self._vm_id, "ssh_user": self._ssh_user},
            )

        needs_overlay = any(not ws.writable for ws in workspace_mounts)
        self._ensure_9p_workspace_support(need_overlay=needs_overlay)

        for index, ws in enumerate(workspace_mounts):
            tag = ws.resolved_tag(index)
            guest_path = shlex.quote(ws.guest_path)
            qtag = shlex.quote(tag)

            if ws.writable:
                mount_script = (
                    f"modprobe 9p 2>/dev/null; "
                    f"modprobe 9pnet_virtio 2>/dev/null; "
                    f"mkdir -p {guest_path} && "
                    f"mount -t 9p -o trans=virtio,version=9p2000.L,rw "
                    f"{qtag} {guest_path}"
                )
            else:
                lower = shlex.quote(f"/mnt/.smolvm-ws-{tag}")
                upper = shlex.quote(f"/tmp/.smolvm-ws-{tag}-upper")
                work = shlex.quote(f"/tmp/.smolvm-ws-{tag}-work")
                mount_script = (
                    f"modprobe 9p 2>/dev/null; "
                    f"modprobe 9pnet_virtio 2>/dev/null; "
                    f"modprobe overlay 2>/dev/null; "
                    f"mkdir -p {lower} {upper} {work} {guest_path} && "
                    f"mount -t 9p -o trans=virtio,version=9p2000.L,ro "
                    f"{qtag} {lower} && "
                    f"mount -t overlay overlay "
                    f"-o lowerdir={lower},upperdir={upper},workdir={work} "
                    f"{guest_path}"
                )
            result = self._ssh.run(mount_script, timeout=15)
            if result.exit_code != 0:
                stderr = result.stderr.strip()
                if "unknown filesystem type" in stderr or "No such device" in stderr:
                    raise SmolVMError(
                        "Guest kernel does not support 9p or overlay filesystem. "
                        "The guest image needs CONFIG_NET_9P, CONFIG_NET_9P_VIRTIO, "
                        "CONFIG_9P_FS, and CONFIG_OVERLAY_FS kernel options.",
                        {"vm_id": self._vm_id, "mount_tag": tag, "stderr": stderr},
                    )
                raise SmolVMError(
                    f"Failed to mount workspace '{tag}' at {ws.guest_path}",
                    {
                        "vm_id": self._vm_id,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.strip(),
                        "stderr": stderr,
                    },
                )
            logger.info(
                "VM %s: mounted workspace '%s' at %s (%s)",
                self._vm_id,
                tag,
                ws.guest_path,
                "writable" if ws.writable else "overlay",
            )

    def _ensure_9p_workspace_support(self, *, need_overlay: bool = True) -> None:
        """Best-effort repair for Ubuntu cloud images missing 9p modules."""
        assert self._ssh is not None  # noqa: S101 — caller guarantees SSH ready

        # Fast path: built-in filesystems show up in /proc/filesystems
        # regardless of /lib/modules/$(uname -r). Our universal microvm
        # kernel has 9p and overlay =y (no modules) and intentionally
        # ships without /lib/modules, so the modprobe-based probe below
        # would fail with "module not found" and trigger the Ubuntu
        # apt-install fallback — which is itself irrelevant for a
        # built-in kernel. Check the kernel's actual capability list
        # first; only fall through to modprobe if the filesystem isn't
        # already registered.
        fs_check = self._ssh.run("cat /proc/filesystems", timeout=15)
        if fs_check.exit_code == 0:
            fs_lines = fs_check.stdout
            has_9p = "\t9p\n" in fs_lines
            has_overlay = "\toverlay\n" in fs_lines
            if has_9p and (not need_overlay or has_overlay):
                return

        overlay_probe = " && modprobe overlay 2>/dev/null" if need_overlay else ""
        probe_script = (
            f"modprobe 9p 2>/dev/null && modprobe 9pnet_virtio 2>/dev/null{overlay_probe}"
        )
        probe = self._ssh.run(probe_script, timeout=15)
        if probe.exit_code == 0:
            return

        overlay_modprobe = "\nmodprobe overlay" if need_overlay else ""
        install_script = (
            r"""
set -eu
. /etc/os-release 2>/dev/null || ID=
if [ "${ID:-}" != "ubuntu" ] || ! command -v apt-get >/dev/null 2>&1; then
  exit 42
fi
export DEBIAN_FRONTEND=noninteractive
APT_LOCKS="/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock"
deadline=$(( $(date +%s) + 120 ))
while command -v fuser >/dev/null 2>&1 && fuser $APT_LOCKS >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Timed out waiting for apt/dpkg locks" >&2
    exit 43
  fi
  sleep 2
done
APT_OPTS='-o DPkg::Lock::Timeout=120 -o Acquire::Retries=3'
apt-get $APT_OPTS update -qq
apt-get $APT_OPTS install -y -qq "linux-modules-extra-$(uname -r)"
modprobe 9p
modprobe 9pnet_virtio""".strip()
            + overlay_modprobe
        )
        install = self._ssh.run(install_script, timeout=240)
        if install.exit_code == 0:
            logger.info(
                "VM %s: installed Ubuntu linux-modules-extra for workspace mounts",
                self._vm_id,
            )
            return

        missing = "9p or overlay" if need_overlay else "9p"
        raise SmolVMError(
            f"Cannot mount workspaces: guest is missing {missing} kernel support. "
            "Ubuntu guests can usually be repaired by installing "
            "linux-modules-extra-$(uname -r); this guest could not be repaired "
            "automatically.",
            {
                "vm_id": self._vm_id,
                "probe_exit_code": probe.exit_code,
                "probe_stderr": probe.stderr.strip(),
                "install_exit_code": install.exit_code,
                "install_stdout": install.stdout.strip(),
                "install_stderr": install.stderr.strip(),
            },
        )

    def _reset_runtime_state(self, *, close_ssh: bool = True) -> None:
        """Clear cached runtime connection state after lifecycle changes."""
        if self._control_channel is not None and self._control_channel is not self._ssh:
            self._control_channel.close()
        self._control_channel = None
        self._control_ready = False
        if close_ssh and self._ssh is not None:
            self._ssh.close()
        if close_ssh:
            self._ssh = None
        self._ssh_ready = False
        if hasattr(self, "_probed_endpoint"):
            self._probed_endpoint = None

    def _ensure_control_cache_attrs(self) -> None:
        """Initialize control-channel cache fields for legacy test fixtures."""
        if not hasattr(self, "_control_channel"):
            self._control_channel = self._ssh if getattr(self, "_ssh_ready", False) else None
        if not hasattr(self, "_control_ready"):
            self._control_ready = self._control_channel is not None

    def _guest_shell_kind(self) -> ShellKind:
        """Pick the SSHClient login-shell flavor for this guest OS.

        Windows guests get ``powershell``; everything else gets the POSIX
        ``sh`` wrap (byte-identical to the pre-Phase-2 behavior).
        """
        if self._info.config.guest_os is GuestOS.WINDOWS:
            return "powershell"
        return "sh"

    def _new_ssh_client(
        self,
        *,
        host: str,
        port: int = 22,
        key_path: str | None = None,
        connect_timeout: int = 10,
    ) -> SSHClient:
        """Construct an SSHClient with this VM's auth + shell flavor.

        Single source of truth for SSH-client construction so password
        auth, key auth, and shell-kind dispatch stay consistent across
        the five call sites (sync/async start, env injection, workspace
        mounts, candidate probing).

        When ``self._ssh_password`` is set, it takes precedence over any
        per-call ``key_path``: paramiko's ``connect()`` prefers
        ``key_filename`` over ``password`` and would silently ignore the
        password if both were passed, which leaves Windows POC users
        wondering why their password never gets tried.

        ``connect_timeout`` defaults to 10s for one-shot operations; the
        async wait_for_ssh polling loop passes a tighter value so each
        in-loop retry fails fast.
        """
        if self._ssh_password is not None:
            effective_key_path: str | None = None
        else:
            effective_key_path = key_path
        return SSHClient(
            host=host,
            user=self._ssh_user,
            port=port,
            key_path=effective_key_path,
            password=self._ssh_password,
            connect_timeout=connect_timeout,
            shell_kind=self._guest_shell_kind(),
        )

    def _ensure_control_for_file_transfer(self) -> CommChannel:
        """Return a ready control channel for file transfer operations."""
        return self._ensure_control_for_operation(action="transfer files")

    def _ensure_ssh_for_env(self, *, timeout: float = _DEFAULT_RUN_READY_TIMEOUT) -> SSHClient:
        """Return a ready SSH client for env operations on a running VM."""
        return self._ensure_ssh_for_operation(
            action="manage environment variables",
            timeout=timeout,
        )

    def _ensure_control_for_operation(
        self,
        *,
        action: str,
        timeout: float = _DEFAULT_RUN_READY_TIMEOUT,
    ) -> CommChannel:
        """Return a ready command/file-transfer control channel."""
        prefix = f"Cannot {action}"
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"{prefix} in sandbox '{self._vm_id}' because it is "
                f"{self._info.status.value}; start it with "
                f"'smolvm sandbox start {self._vm_id}'.",
                {"vm_id": self._vm_id},
            )
        resolution = self._resolve_channel()
        if not self.can_run_commands():
            raise CommandExecutionUnavailableError(
                vm_id=self._vm_id,
                reason="VM config does not advertise a command-capable boot path.",
                remediation=self._command_exec_remediation(),
            )
        if resolution.kind == "ssh" and self._info.network is None:
            raise SmolVMError(
                f"{prefix} in sandbox '{self._vm_id}' over SSH because it has no network; "
                f"remove it with 'smolvm sandbox delete {self._vm_id}' and create it again.",
                {"vm_id": self._vm_id},
            )

        self._ensure_control_cache_attrs()
        if not self._control_ready:
            self._wait_for_ready(timeout=timeout)

        if self._control_channel is None:
            raise SmolVMError(
                f"{prefix}: control channel is not initialized",
                {"vm_id": self._vm_id},
            )

        return self._control_channel

    def _ensure_ssh_for_operation(
        self,
        *,
        action: str,
        timeout: float = _DEFAULT_RUN_READY_TIMEOUT,
    ) -> SSHClient:
        """Return a ready SSH client for operations that need guest SSH."""
        prefix = f"Cannot {action}"
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"{prefix}: VM is {self._info.status.value}",
                {"vm_id": self._vm_id},
            )
        if not self.can_run_commands():
            raise CommandExecutionUnavailableError(
                vm_id=self._vm_id,
                reason=(
                    "VM config does not advertise an SSH-capable boot path, "
                    "so guest SSH is not guaranteed to start."
                ),
                remediation=self._command_exec_remediation(),
            )
        if self._info.network is None:
            raise SmolVMError(
                f"{prefix}: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        if not self._ssh_ready:
            self._wait_for_ssh_over_network(timeout=timeout)

        if self._ssh is None:
            raise SmolVMError(
                f"{prefix}: SSH client is not initialized",
                {"vm_id": self._vm_id},
            )

        return self._ssh

    def _is_port_reachable(self, host: str, port: int, timeout: float = 0.5) -> bool:
        """Check if a TCP port is open."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (OSError, TimeoutError):
            return False

    def _refresh_info(self) -> None:
        """Refresh the cached VMInfo from the state store."""
        self._info = self._sdk.get(self._vm_id)

    def _ssh_endpoint(self) -> tuple[str, int]:
        """Return the preferred host/port endpoint for guest SSH.

        If localhost port forwarding is configured but unreachable
        (e.g. due to Linux networking issues), falls back to direct
        guest IP. The result is cached for the VM session lifetime.
        """
        # Return cached endpoint if available and valid
        if hasattr(self, "_probed_endpoint") and self._probed_endpoint:
            return self._probed_endpoint

        candidates = self._ssh_endpoints()
        if not candidates:
            raise SmolVMError(
                "No SSH endpoints available",
                {"vm_id": self._vm_id},
            )

        # Probe candidates in order
        for host, port in candidates:
            # If 127.0.0.1, we probe to ensure forwarding works.
            # If it's a direct IP, we assume it's the fallback.
            # (We probe all to be safe, with short timeout)
            if self._is_port_reachable(host, port, timeout=0.2):
                self._probed_endpoint = (host, port)
                return (host, port)

        # If none reachable (e.g. boot not finished), return the first one
        # to let standard SSH retries handle it.
        return candidates[0]

    def _ssh_endpoints(self) -> list[tuple[str, int]]:
        """Return SSH endpoint candidates in preferred order."""
        if self._info.network is None:
            raise SmolVMError(
                "VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        endpoints: list[tuple[str, int]] = []
        ssh_host_port = self._info.network.ssh_host_port
        if isinstance(ssh_host_port, int):
            endpoints.append(("127.0.0.1", ssh_host_port))
        endpoints.append((self._info.network.guest_ip, 22))

        unique: list[tuple[str, int]] = []
        for endpoint in endpoints:
            if endpoint in unique:
                continue
            unique.append(endpoint)
        return unique

    def _attempt_ssh_candidates(
        self,
        attempts: list[tuple[str, int, str | None]],
        *,
        deadline: float,
        errors: list[str],
        as_control: bool = False,
    ) -> bool:
        """Try a sequence of SSH endpoint/key combinations.

        Returns:
            ``True`` once SSH becomes ready, otherwise ``False``.
        """
        for index, (host, port, key_path) in enumerate(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            attempts_left = len(attempts) - index
            endpoint_timeout = max(0.5, remaining / attempts_left)
            client = self._ssh
            if (
                client is None
                or client.host != host
                or client.port != port
                or client.key_path != key_path
            ):
                client = self._new_ssh_client(
                    host=host,
                    port=port,
                    key_path=key_path,
                )

            try:
                client.wait_for_ssh(timeout=endpoint_timeout)
                self._ssh = client
                self._ssh_ready = True
                if as_control:
                    self._control_channel = client
                    self._control_ready = True
                if self._ssh_key_path is None and key_path is not None:
                    self._ssh_key_path = key_path
                    logger.debug(
                        "VM %s: SSH ready using key path %s",
                        self._vm_id,
                        key_path,
                    )
                return True
            except OperationTimeoutError as e:
                key_label = "agent/default-auth" if key_path is None else f"key={key_path}"
                errors.append(f"{host}:{port} [{key_label}] ({e.message})")

        return False

    def _resolve_channel(self) -> ChannelResolution:
        """Resolve which control channel this VM should use."""
        config = self._info.config
        try:
            return resolve_comm_channel(
                requested=getattr(self, "_comm_channel_request", None),
                config_channel=getattr(config, "comm_channel", None),
                backend=getattr(config, "backend", None),
                guest_os=getattr(config, "guest_os", None),
            )
        except VsockNotSupportedError as exc:
            raise SmolVMError(
                _vsock_not_supported_message(self._vm_id, exc),
                {"vm_id": self._vm_id, **exc.details},
            ) from exc

    def _try_vsock_ready(self, timeout: float) -> bool:
        """Probe the guest vsock agents; on success cache the control channel.

        Returns True if the agent answered within *timeout*. Requires a
        reserved CID (``config.vsock``) — without one (e.g. a VM created
        before vsock was enabled) vsock can't be used.
        """
        self._refresh_info()
        vsock = getattr(self._info.config, "vsock", None)
        if vsock is None:
            return False
        backend = getattr(self._info.config, "backend", None)
        if backend == BACKEND_FIRECRACKER:
            uds_path = vsock.uds_path or getattr(self._info, "vsock_uds_path", None)
            if not uds_path:
                return False
            channel: CommChannel = RustHttpVsockChannel.from_uds(uds_path)
        else:
            channel = RustHttpVsockChannel.from_cid(vsock.guest_cid)

        try:
            channel.wait_ready(timeout=timeout)
        except (OperationTimeoutError, SmolVMError, OSError):
            channel.close()
            return False
        self._control_channel = channel
        self._control_ready = True
        return True

    def _wait_for_ready(self, timeout: float) -> None:
        """Wait until the resolved control channel is ready.

        It tries vsock when that is the resolved channel, falling back to SSH
        only when channel selection was automatic.
        """
        if self._control_ready:
            return
        resolution = self._resolve_channel()
        if resolution.kind == "vsock":
            deadline = time.monotonic() + timeout
            probe = timeout
            if resolution.allow_fallback:
                probe = min(timeout, _VSOCK_AUTO_PROBE_TIMEOUT)
            if self._try_vsock_ready(probe):
                return
            if not resolution.allow_fallback:
                raise OperationTimeoutError(
                    "wait_for_ready: the guest vsock agent did not respond", timeout
                )
            logger.info("VM %s: vsock agent not reachable, falling back to SSH", self._vm_id)
            self._wait_for_ssh_over_network(
                max(1.0, deadline - time.monotonic()),
                as_control=True,
            )
            return
        self._wait_for_ssh_over_network(timeout, as_control=True)

    def _wait_for_ssh_over_network(self, timeout: float, *, as_control: bool = False) -> None:
        """Wait for SSH across available network endpoints."""
        self._sdk.ensure_network_connectivity(self._info)
        endpoints = self._ssh_endpoints()

        # Prefer the already selected client first, then try remaining candidates.
        ordered_endpoints = endpoints
        if self._ssh is not None:
            current = (self._ssh.host, self._ssh.port)
            ordered_endpoints = [current]
            ordered_endpoints.extend(endpoint for endpoint in endpoints if endpoint != current)

        attempts: list[tuple[str, int, str | None]] = []
        if self._ssh is not None:
            attempts.append((self._ssh.host, self._ssh.port, self._ssh.key_path))

        # Try configured key, or default SSH auth (no -i).
        primary_key = self._ssh_key_path
        for host, port in ordered_endpoints:
            attempt = (host, port, primary_key)
            if attempt in attempts:
                continue
            attempts.append(attempt)

        # When no explicit key was provided, also try the default SmolVM key
        # (~/.smolvm/keys/id_ed25519). VMs created via auto-config always have
        # this public key injected, so reconnecting via from_id() without an
        # explicit ssh_key_path should still authenticate correctly.
        if primary_key is None:
            from smolvm.utils import ensure_ssh_key

            try:
                default_key, _ = ensure_ssh_key()
                default_key_str = str(default_key)
                for host, port in ordered_endpoints:
                    attempt = (host, port, default_key_str)
                    if attempt in attempts:
                        continue
                    attempts.append(attempt)
            except Exception:
                pass  # Key doesn't exist or can't be created — skip silently

        errors: list[str] = []
        deadline = time.monotonic() + timeout
        if self._attempt_ssh_candidates(
            attempts,
            deadline=deadline,
            errors=errors,
            as_control=as_control,
        ):
            return

        if as_control:
            self._control_ready = False
        else:
            self._ssh_ready = False
        detail = "; ".join(errors) if errors else "no endpoint attempts completed"
        raise OperationTimeoutError(f"wait_for_ssh: {detail}", timeout)

    def _cleanup_local_forwards(self) -> None:
        """Best-effort cleanup for localhost-only guest port forwards."""
        if not self._local_forwards:
            return

        guest_ip: str | None = None
        with suppress(Exception):
            self._refresh_info()
            if self._info.network is not None:
                guest_ip = self._info.network.guest_ip

        for key, tracked in list(self._local_forwards.items()):
            try:
                self._cleanup_local_forward(tracked, guest_ip=guest_ip)
            except Exception:
                logger.warning(
                    "Failed to cleanup local forward localhost:%d -> guest:%d for VM %s",
                    tracked.host_port,
                    tracked.guest_port,
                    self._vm_id,
                )
            finally:
                self._local_forwards.pop(key, None)

    def _should_try_nftables_local_forward(self) -> bool:
        """Return whether localhost exposure should attempt host nftables first."""
        config = getattr(self._info, "config", None)
        backend = getattr(config, "backend", None)
        return backend not in {BACKEND_QEMU, BACKEND_LIBKRUN}

    @staticmethod
    def _find_available_local_port() -> int:
        """Return an available TCP port bound on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _allocate_local_port(self, exclude: set[int] | None = None) -> int:
        """Allocate an unused localhost port not already tracked."""
        excluded = set(exclude or ())
        used = {forward.host_port for forward in self._local_forwards.values()}

        for _ in range(_LOCAL_FORWARD_MAX_PORT_ATTEMPTS):
            candidate = self._find_available_local_port()
            if candidate in excluded or candidate in used:
                continue
            return candidate

        raise SmolVMError(
            "Failed to allocate an available localhost port",
            {"vm_id": self._vm_id, "excluded_ports": sorted(excluded | used)},
        )

    @staticmethod
    def _probe_local_forward(host_port: int, timeout: float = _LOCAL_FORWARD_PROBE_TIMEOUT) -> bool:
        """Check whether localhost:host_port is currently accepting TCP connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=0.5):
                    return True
            except OSError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_LOCAL_FORWARD_PROBE_INTERVAL, remaining))
        return False

    def _start_local_tunnel(self, host_port: int, guest_port: int) -> subprocess.Popen[str]:
        """Start an SSH localhost tunnel from host_port to guest_port."""
        if self._info.network is None:
            raise SmolVMError(
                "Cannot create SSH tunnel: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        ssh_host, ssh_port = self._ssh_endpoint()
        cmd = [
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            "-p",
            str(ssh_port),
            "-L",
            f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
        ]
        key_path = self._ssh_key_path
        if key_path is None:
            from smolvm.utils import ensure_ssh_key

            try:
                default_key, _ = ensure_ssh_key()
                key_path = str(default_key)
            except Exception:
                pass
        if key_path:
            cmd.extend(["-i", key_path])
        cmd.append(f"{self._ssh_user}@{ssh_host}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise SmolVMError(
                "ssh binary not found. Install openssh-client.",
                {"vm_id": self._vm_id},
            ) from None
        except OSError as e:
            raise SmolVMError(
                f"Failed to start SSH tunnel: {e}",
                {"vm_id": self._vm_id},
            ) from e

        deadline = time.monotonic() + _LOCAL_TUNNEL_START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = ""
                if proc.stderr is not None:
                    with suppress(Exception):
                        stderr = proc.stderr.read().strip()
                raise SmolVMError(
                    "SSH tunnel exited before becoming ready."
                    + (f" stderr: {stderr}" if stderr else ""),
                    {"vm_id": self._vm_id, "host_port": host_port, "guest_port": guest_port},
                )
            if self._probe_local_forward(host_port, timeout=0.3):
                return proc
            time.sleep(_LOCAL_FORWARD_PROBE_INTERVAL)

        self._stop_local_tunnel(proc)
        raise SmolVMError(
            f"SSH tunnel did not become ready on localhost:{host_port} within "
            f"{_LOCAL_TUNNEL_START_TIMEOUT:.1f}s",
            {"vm_id": self._vm_id, "host_port": host_port, "guest_port": guest_port},
        )

    @staticmethod
    def _stop_local_tunnel(proc: subprocess.Popen[str] | None) -> None:
        """Best-effort shutdown for an SSH tunnel process."""
        if proc is None:
            return

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(Exception):
                    proc.wait(timeout=2.0)

        if proc.stderr is not None:
            with suppress(Exception):
                proc.stderr.close()

    def _cleanup_local_forward(
        self,
        forward: _LocalForward,
        *,
        guest_ip: str | None = None,
    ) -> None:
        """Remove one tracked localhost exposure."""
        if forward.transport == "ssh_tunnel":
            self._stop_local_tunnel(forward.tunnel_proc)
            return

        if guest_ip is None:
            with suppress(Exception):
                self._refresh_info()
                if self._info.network is not None:
                    guest_ip = self._info.network.guest_ip

        if guest_ip is None:
            logger.warning(
                "Skipping nftables cleanup for localhost:%d -> guest:%d on VM %s "
                "because guest network info is unavailable",
                forward.host_port,
                forward.guest_port,
                self._vm_id,
            )
            return

        self._sdk.network.cleanup_local_port_forward(
            vm_id=self._vm_id,
            guest_ip=guest_ip,
            host_port=forward.host_port,
            guest_port=forward.guest_port,
        )

    def _command_exec_remediation(self) -> str:
        """Return actionable guidance when command execution is unavailable."""
        return (
            "Use `SmolVM()` auto-config mode for an SSH-ready image, or build one with "
            "`ImageBuilder` when you need custom guest composition."
        )

    def __repr__(self) -> str:
        return f"SmolVM(vm_id={self._vm_id!r}, status={self._info.status.value!r})"
