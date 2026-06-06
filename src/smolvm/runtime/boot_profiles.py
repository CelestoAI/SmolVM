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

"""Internal kernel boot-profile definitions for SmolVM images.

After 0.0.14a0 SmolVM ships a single universal microvm kernel
([build-microvm-kernel.yml](.github/workflows/build-microvm-kernel.yml)) that boots
under Firecracker (virtio-MMIO) AND QEMU/libkrun (virtio-PCI). The previous
external sources (Firecracker-CI on S3, Ubuntu cloud-images vmlinuz) are
retired. Boot profiles still distinguish *boot modes* (direct kernel vs
kernel+initramfs for user-supplied S3 images), but the kernel artifact is
the same across all SmolVM-built profiles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_LIBKRUN, BACKEND_QEMU

if TYPE_CHECKING:
    from smolvm.images.published import Arch as PublishedArch

# Latency-safe kernel cmdline trims for minimal headless virtio/KVM guests.
# - tsc=reliable      : trust the host TSC clocksource, skip calibration checks.
# - no_timer_check    : skip the legacy "is the timer IRQ arriving" probe.
# Both are safe on any KVM virtio guest and shave kernel-init time. `quiet` is
# added separately (it suppresses console printk, which is useful to keep when
# debugging a stuck boot — see _quiet_flag). `acpi=off` is deliberately NOT
# defaulted: it saves ~70ms but isn't universally safe (clean shutdown / IRQ
# routing), so it stays opt-in per image.
_SAFE_TRIM_FLAGS = "tsc=reliable no_timer_check"


def _quiet_flag() -> str:
    """Return ``" quiet"`` unless verbose boot was requested.

    Set ``SMOLVM_VERBOSE_BOOT=1`` to keep full kernel console output when
    debugging a boot that hangs or panics; the default suppresses it to cut
    time spent formatting printk over the serial line.
    """
    verbose = os.environ.get("SMOLVM_VERBOSE_BOOT", "").strip().lower()
    if verbose in {"1", "true", "yes", "on"}:
        return ""
    return " quiet"


def safe_kernel_trim_args(*, quiet: bool | None = None) -> tuple[str, ...]:
    """Return latency-safe kernel command-line trims.

    ``quiet=None`` preserves SmolVM's default behavior: include ``quiet``
    unless ``SMOLVM_VERBOSE_BOOT`` asks for verbose kernel output. Pass
    ``quiet=True`` or ``quiet=False`` to force either behavior for a custom
    boot profile.
    """
    args = _SAFE_TRIM_FLAGS.split()
    if quiet is None:
        quiet_arg = _quiet_flag().strip()
        if quiet_arg:
            args.append(quiet_arg)
    elif quiet:
        args.append("quiet")
    return tuple(args)


_MICROVM_DIRECT_FIRECRACKER_BASE = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw"


class KernelBootProfile(str, Enum):
    """Internal kernel artifact/boot-mode families."""

    MICROVM_DIRECT = "microvm_direct"
    QEMU_DESKTOP_INITRAMFS = "qemu_desktop_initramfs"


@dataclass(frozen=True, slots=True)
class BootProfileSpec:
    """Structured boot-profile metadata for internal image selection."""

    profile: KernelBootProfile
    boot_mode: Literal["direct_kernel", "kernel_plus_initramfs"]

    def base_boot_args_for_backend(self, backend: str, arch: str) -> str:
        """Return base boot args before backend manager normalization."""
        normalized_arch = normalize_arch(arch)
        if not self.supports_backend(backend):
            raise ValueError(
                f"Boot profile {self.profile.value} does not support backend {backend}"
            )

        if self.profile is KernelBootProfile.MICROVM_DIRECT:
            trims = " ".join(safe_kernel_trim_args())
            if backend in {BACKEND_QEMU, BACKEND_LIBKRUN}:
                console = "ttyAMA0" if normalized_arch == "aarch64" else "ttyS0"
                return f"console={console} reboot=k panic=1 {trims} init=/init"
            # Firecracker: keep pci=off + root=/dev/vda, append the same trims.
            return f"{_MICROVM_DIRECT_FIRECRACKER_BASE} {trims} init=/init"

        console = "ttyAMA0" if normalized_arch == "aarch64" else "ttyS0"
        return f"console={console} reboot=k panic=1"

    def supports_backend(self, backend: str) -> bool:
        """Return whether the profile supports the runtime backend."""
        if self.profile is KernelBootProfile.MICROVM_DIRECT:
            return backend in {BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN}
        return backend == BACKEND_QEMU


_BOOT_PROFILE_SPECS: dict[KernelBootProfile, BootProfileSpec] = {
    KernelBootProfile.MICROVM_DIRECT: BootProfileSpec(
        profile=KernelBootProfile.MICROVM_DIRECT,
        boot_mode="direct_kernel",
    ),
    KernelBootProfile.QEMU_DESKTOP_INITRAMFS: BootProfileSpec(
        profile=KernelBootProfile.QEMU_DESKTOP_INITRAMFS,
        boot_mode="kernel_plus_initramfs",
    ),
}


def normalize_arch(arch: str) -> str:
    """Normalize host architecture values to SmolVM kernel keys."""
    normalized = arch.lower()
    if normalized in {"x86_64", "amd64"}:
        return "x86_64"
    if normalized in {"arm64", "aarch64"}:
        return "aarch64"
    raise ValueError(f"Unsupported host architecture '{arch}'")


def to_published_arch(arch: str) -> PublishedArch:
    """Map kernel-style arch (`x86_64`/`aarch64`) to SmolVM-style (`amd64`/`arm64`).

    Bridges the two arch namings that live next to each other in this codebase:
    ``boot_profiles`` and Linux internals use ``x86_64``/``aarch64``; the
    published-image manifest and the auto-config flow use ``amd64``/``arm64``.
    """
    normalized = normalize_arch(arch)
    return "amd64" if normalized == "x86_64" else "arm64"


def get_boot_profile_spec(profile: KernelBootProfile) -> BootProfileSpec:
    """Return the metadata for the requested internal boot profile."""
    return _BOOT_PROFILE_SPECS[profile]


def resolve_kernel_path(
    arch: str,
    backend: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Return the local path to the SmolVM-built base kernel.

    Picks the binary format by backend:
    - ``firecracker`` / ``libkrun`` → ELF (their kernel loaders require it)
    - ``qemu`` → Image / bzImage (QEMU on aarch64 ``virt`` empirically refuses
      to boot a Linux ELF; on x86 q35 either format works but we standardise
      on Image for consistency)

    Downloads (with SHA-256 verification) on cache miss, returns a cached
    path on hit. Same source build under both formats; what differs is just
    the container.
    """
    from smolvm.images.published import _kernel_format_for_vmm, ensure_base_kernel

    fmt = _kernel_format_for_vmm(backend)  # type: ignore[arg-type]
    return ensure_base_kernel(to_published_arch(arch), fmt, cache_dir=cache_dir)
