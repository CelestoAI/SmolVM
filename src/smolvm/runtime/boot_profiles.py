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

# SmolVM-native kernel bundle. Unlike the other profiles above, the URL here
# points at a `.tar.zst` archive that contains the kernel image plus
# metadata (config, SHA256SUMS, LINUX_VERSION) — not a raw vmlinux. The
# archive must be fetched via ImageManager.ensure_kernel_bundle() and
# extracted before the kernel inside can be used for boot.
#
# The version pinned here must correspond to a published kernel-v* GitHub
# Release. See kernel/README.md for the release flow.
SMOLVM_NATIVE_KERNEL_VERSION = "1.0.0"
SMOLVM_NATIVE_RELEASE_HOST = "https://github.com/CelestoAI/SmolVM/releases/download"
SMOLVM_NATIVE_BUNDLE_URLS: dict[str, str] = {
    "x86_64": (
        f"{SMOLVM_NATIVE_RELEASE_HOST}/"
        f"kernel-v{SMOLVM_NATIVE_KERNEL_VERSION}/"
        f"smolvm-kernel-v{SMOLVM_NATIVE_KERNEL_VERSION}-x86_64.tar.zst"
    ),
    "aarch64": (
        f"{SMOLVM_NATIVE_RELEASE_HOST}/"
        f"kernel-v{SMOLVM_NATIVE_KERNEL_VERSION}/"
        f"smolvm-kernel-v{SMOLVM_NATIVE_KERNEL_VERSION}-aarch64.tar.zst"
    ),
}
# TODO(kernel-v1.0.0): populate with real sha256s captured by the first
# kernel-release.yml run. Until these are set, ensure_kernel_bundle() will
# refuse to trust the download — which is the correct behavior for a
# placeholder release pointer.
SMOLVM_NATIVE_BUNDLE_SHA256: dict[str, str | None] = {
    "x86_64": None,
    "aarch64": None,
}

_MICROVM_DIRECT_FIRECRACKER_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
)


class KernelBootProfile(str, Enum):
    """Internal kernel artifact/boot-mode families."""

    MICROVM_DIRECT = "microvm_direct"
    QEMU_DESKTOP_INITRAMFS = "qemu_desktop_initramfs"
    # Purpose-built SmolVM kernel with virtiofs/FUSE/overlayfs compiled in
    # and MODULES=n. Boots on QEMU + Firecracker. libkrun ships its own
    # kernel and therefore does not use this profile.
    SMOLVM_NATIVE = "smolvm_native"


@dataclass(frozen=True, slots=True)
class BootProfileSpec:
    """Structured boot-profile metadata for internal image selection."""

    profile: KernelBootProfile
    # URL-per-arch for the kernel artifact. For the flat-file profiles
    # (MICROVM_DIRECT, QEMU_DESKTOP_INITRAMFS) this is a direct kernel
    # binary. For SMOLVM_NATIVE this is a `.tar.zst` bundle that must be
    # fetched and extracted via ImageManager.ensure_kernel_bundle().
    kernel_url_by_arch: dict[str, str]
    boot_mode: Literal["direct_kernel", "kernel_plus_initramfs"]
    # True for bundle-style profiles (tarball containing kernel + metadata).
    # When True, bundle_sha256_by_arch MUST be populated with non-None
    # values for each supported arch before the profile becomes usable.
    is_bundle: bool = False
    bundle_sha256_by_arch: dict[str, str | None] | None = None

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

        if self.profile is KernelBootProfile.SMOLVM_NATIVE:
            # The SmolVM kernel has VIRTIO_FS / FUSE / OVERLAY compiled in
            # and MODULES=n — no modprobe ever, no module mismatch. Args
            # match the Alpine rootfs init contract (builder.SSH_BOOT_ARGS).
            console = "ttyAMA0" if normalized_arch == "aarch64" else "ttyS0"
            return f"console={console} reboot=k panic=1 pci=off root=/dev/vda rw init=/init"

        console = "ttyAMA0" if normalized_arch == "aarch64" else "ttyS0"
        return f"console={console} reboot=k panic=1"

    def supports_backend(self, backend: str) -> bool:
        """Return whether the profile supports the runtime backend."""
        if self.profile is KernelBootProfile.MICROVM_DIRECT:
            return backend in {BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN}
        if self.profile is KernelBootProfile.SMOLVM_NATIVE:
            # libkrun supplies its own embedded kernel; our kernel is not
            # plugged into it. QEMU + Firecracker consume the bundle.
            return backend in {BACKEND_FIRECRACKER, BACKEND_QEMU}
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
    KernelBootProfile.SMOLVM_NATIVE: BootProfileSpec(
        profile=KernelBootProfile.SMOLVM_NATIVE,
        kernel_url_by_arch=SMOLVM_NATIVE_BUNDLE_URLS,
        boot_mode="direct_kernel",
        is_bundle=True,
        bundle_sha256_by_arch=SMOLVM_NATIVE_BUNDLE_SHA256,
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
    """Return the kernel URL for a boot profile and architecture.

    For flat-file profiles this URL points at a kernel binary directly.
    For bundle profiles (see ``BootProfileSpec.is_bundle``) the URL
    resolves to a ``.tar.zst`` archive that must be fetched via
    ``ImageManager.ensure_kernel_bundle()``.
    """
    normalized_arch = normalize_arch(arch)
    spec = get_boot_profile_spec(profile)
    return spec.kernel_url_by_arch[normalized_arch]


def resolve_bundle_sha256(profile: KernelBootProfile, arch: str) -> str | None:
    """Return the pinned sha256 for a bundle-style boot profile.

    Returns ``None`` both for non-bundle profiles and for bundle profiles
    whose sha256 has not yet been populated (e.g. a placeholder release
    pointer). Callers are expected to treat ``None`` as "no integrity
    check available, refuse to trust the artifact".
    """
    normalized_arch = normalize_arch(arch)
    spec = get_boot_profile_spec(profile)
    if not spec.is_bundle or spec.bundle_sha256_by_arch is None:
        return None
    return spec.bundle_sha256_by_arch.get(normalized_arch)
