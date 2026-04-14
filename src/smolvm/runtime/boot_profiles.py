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

"""Internal kernel boot-profile definitions for SmolVM images."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_LIBKRUN, BACKEND_QEMU

# Firecracker-compatible uncompressed kernels.
FIRECRACKER_KERNEL_URLS: dict[str, str] = {
    "x86_64": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/x86_64/vmlinux-5.10.198",
    "aarch64": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/aarch64/vmlinux-5.10.198",
}

# Future desktop-capable QEMU path: distro kernels that are expected to boot
# together with a matching initramfs/modules set.
QEMU_DESKTOP_KERNEL_URLS: dict[str, str] = {
    "x86_64": (
        "https://cloud-images.ubuntu.com/noble/current/unpacked/"
        "noble-server-cloudimg-amd64-vmlinuz-generic"
    ),
    "aarch64": (
        "https://cloud-images.ubuntu.com/noble/current/unpacked/"
        "noble-server-cloudimg-arm64-vmlinuz-generic"
    ),
}

_MICROVM_DIRECT_FIRECRACKER_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
)


class KernelBootProfile(str, Enum):
    """Internal kernel artifact/boot-mode families."""

    MICROVM_DIRECT = "microvm_direct"
    QEMU_DESKTOP_INITRAMFS = "qemu_desktop_initramfs"


@dataclass(frozen=True, slots=True)
class BootProfileSpec:
    """Structured boot-profile metadata for internal image selection."""

    profile: KernelBootProfile
    kernel_url_by_arch: dict[str, str]
    boot_mode: Literal["direct_kernel", "kernel_plus_initramfs"]

    def base_boot_args_for_backend(self, backend: str, arch: str) -> str:
        """Return base boot args before backend manager normalization."""
        normalized_arch = normalize_arch(arch)
        if not self.supports_backend(backend):
            raise ValueError(
                f"Boot profile {self.profile.value} does not support backend {backend}"
            )

        if self.profile is KernelBootProfile.MICROVM_DIRECT:
            if backend in {BACKEND_QEMU, BACKEND_LIBKRUN}:
                console = "ttyAMA0" if normalized_arch == "aarch64" else "ttyS0"
                return f"console={console} reboot=k panic=1 init=/init"
            return _MICROVM_DIRECT_FIRECRACKER_BOOT_ARGS

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
        kernel_url_by_arch=FIRECRACKER_KERNEL_URLS,
        boot_mode="direct_kernel",
    ),
    KernelBootProfile.QEMU_DESKTOP_INITRAMFS: BootProfileSpec(
        profile=KernelBootProfile.QEMU_DESKTOP_INITRAMFS,
        kernel_url_by_arch=QEMU_DESKTOP_KERNEL_URLS,
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


def get_boot_profile_spec(profile: KernelBootProfile) -> BootProfileSpec:
    """Return the metadata for the requested internal boot profile."""
    return _BOOT_PROFILE_SPECS[profile]


def resolve_kernel_url(profile: KernelBootProfile, arch: str) -> str:
    """Return the kernel URL for a boot profile and architecture."""
    normalized_arch = normalize_arch(arch)
    spec = get_boot_profile_spec(profile)
    return spec.kernel_url_by_arch[normalized_arch]
