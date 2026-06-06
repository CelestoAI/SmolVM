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

"""Public boot-profile helpers for custom SmolVM images."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from smolvm.runtime.backends import (
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
    resolve_backend,
)
from smolvm.runtime.boot_profiles import normalize_arch, safe_kernel_trim_args

ConsoleMode = Literal["serial", "none"]
RootfsFormat = Literal["raw-ext4", "qcow2"]
BootBackend = Literal["firecracker", "qemu", "libkrun"]
BootArch = Literal["amd64", "arm64"]
BootMode = Literal["direct_kernel", "firmware"]


def _check_kernel_token(label: str, value: str | None) -> str | None:
    """Validate one kernel command-line token value."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} must not be empty")
    if any(ch.isspace() for ch in stripped):
        raise ValueError(f"{label} must be one kernel argument token, got {value!r}")
    return stripped


def _check_required_kernel_token(label: str, value: str | None) -> str:
    """Validate a required kernel command-line token value."""
    checked = _check_kernel_token(label, value)
    if checked is None:
        raise ValueError(f"{label} must not be None")
    return checked


def _check_extra_arg(value: str | None) -> str:
    """Validate one caller-supplied extra kernel argument."""
    checked = _check_kernel_token("extra_args", value)
    if checked is None:
        raise ValueError("extra_args entries must not be None")
    return checked


def _serial_console_for_backend(backend: str, arch: str) -> str:
    """Return the serial console device for a backend/arch pair."""
    normalized_arch = normalize_arch(arch)
    if backend == BACKEND_FIRECRACKER:
        return "ttyS0"
    if backend in {BACKEND_QEMU, BACKEND_LIBKRUN}:
        return "ttyAMA0" if normalized_arch == "aarch64" else "ttyS0"
    raise ValueError(f"Unsupported backend {backend!r}")


@dataclass(frozen=True, slots=True)
class DirectKernelBoot:
    """Render backend-correct direct-kernel boot arguments.

    The root filesystem and init path are caller-owned, but SmolVM owns the
    backend quirks: Firecracker needs ``pci=off``, QEMU/libkrun must not get
    it, and QEMU aarch64 uses ``ttyAMA0`` for the serial console.
    """

    root: str = "/dev/vda"
    init: str | None = "/init"
    rw: bool = True
    console: ConsoleMode = "serial"
    panic: int = 1
    reboot: str = "k"
    safe_trims: bool = True
    quiet: bool | None = None
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.console not in {"serial", "none"}:
            raise ValueError("console must be 'serial' or 'none'")
        if self.panic < 0:
            raise ValueError("panic must be >= 0")

        checked_root = _check_required_kernel_token("root", self.root)
        checked_reboot = _check_required_kernel_token("reboot", self.reboot)
        object.__setattr__(self, "root", checked_root)
        object.__setattr__(self, "init", _check_kernel_token("init", self.init))
        object.__setattr__(self, "reboot", checked_reboot)
        object.__setattr__(
            self,
            "extra_args",
            tuple(_check_extra_arg(arg) for arg in self.extra_args),
        )

    def render(self, *, backend: str, arch: str = "host") -> str:
        """Render this boot profile for a runtime backend and architecture."""
        resolved_backend = resolve_backend(backend)
        resolved_arch = platform.machine() if arch == "host" else arch
        # Validate early so unknown arches fail before returning a partial string.
        normalize_arch(resolved_arch)

        parts: list[str] = []
        if self.console == "serial":
            console = _serial_console_for_backend(resolved_backend, resolved_arch)
            parts.append(f"console={console}")

        parts.extend([f"reboot={self.reboot}", f"panic={self.panic}"])
        if resolved_backend == BACKEND_FIRECRACKER:
            parts.append("pci=off")

        parts.extend([f"root={self.root}", "rw" if self.rw else "ro"])
        if self.init is not None:
            parts.append(f"init={self.init}")

        if self.safe_trims:
            parts.extend(safe_kernel_trim_args(quiet=self.quiet))
        parts.extend(self.extra_args)
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class FirmwareBoot:
    """Marker for images that boot through firmware instead of ``-kernel``."""


class BootImage(BaseModel):
    """A bootable base image that can be turned into an isolated VM later.

    ``BootImage`` describes image artifacts and boot metadata. It does not
    allocate networking, clone disks, resize filesystems, or start a VM.
    """

    name: str
    rootfs_path: Path
    rootfs_format: RootfsFormat
    kernel_path: Path | None = None
    initrd_path: Path | None = None
    boot: DirectKernelBoot | FirmwareBoot | None = None
    boot_args: str | None = None
    backend: BootBackend | None = None
    arch: BootArch | None = None
    ssh_capable: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """Reject blank image names."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped

    @field_validator("boot_args")
    @classmethod
    def _validate_boot_args(cls, value: str | None) -> str | None:
        """Reject blank boot-argument overrides."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("boot_args must not be empty")
        return stripped

    @field_validator("rootfs_path", "kernel_path", "initrd_path")
    @classmethod
    def _validate_existing_file(cls, value: Path | None) -> Path | None:
        """Ensure declared image artifacts exist and are regular files."""
        if value is None:
            return None
        if not value.exists():
            raise ValueError(f"Path does not exist: {value}")
        if not value.is_file():
            raise ValueError(f"Path is not a file: {value}")
        return value

    @model_validator(mode="after")
    def _check_boot_contract(self) -> BootImage:
        """Reject ambiguous boot metadata."""
        if isinstance(self.boot, FirmwareBoot):
            if self.kernel_path is not None:
                raise ValueError("firmware images must not set kernel_path")
            if self.initrd_path is not None:
                raise ValueError("firmware images must not set initrd_path")
            if self.boot_args is not None:
                raise ValueError("firmware images must not set boot_args")
            if self.backend is not None and self.backend != BACKEND_QEMU:
                raise ValueError("firmware images require backend='qemu'")
            return self

        if self.boot is not None and self.boot_args is not None:
            raise ValueError("boot and boot_args are mutually exclusive")
        if self.boot is None and self.boot_args is None:
            raise ValueError("direct-kernel images need boot or boot_args")
        return self

    @property
    def boot_mode(self) -> BootMode:
        """Return the VMConfig boot mode implied by this image."""
        if isinstance(self.boot, FirmwareBoot):
            return "firmware"
        return "direct_kernel"

    def render_boot_args(self, *, backend: str | None = None, arch: str | None = None) -> str:
        """Render or return the direct-kernel command line for this image."""
        if self.boot_mode == "firmware":
            return ""
        if self.boot_args is not None:
            return self.boot_args
        if not isinstance(self.boot, DirectKernelBoot):
            raise ValueError("direct-kernel images need boot or boot_args")

        resolved_backend = backend or self.backend
        if resolved_backend is None:
            raise ValueError("backend is required to render direct-kernel boot args")
        return self.boot.render(backend=resolved_backend, arch=arch or self.arch or "host")

    model_config = ConfigDict(frozen=True)
