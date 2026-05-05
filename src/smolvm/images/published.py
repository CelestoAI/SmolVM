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

"""Published pre-built VM images for SmolVM presets.

Images are built and signed in CI, hosted on GitHub Releases, and pinned
lock-step to the CLI version: SmolVM ``X.Y.Z`` always pulls images from
the ``images-vX.Y.Z`` release tag. The ``MANIFEST`` below is the bundled
catalog the CLI ships with — entries are appended as CI publishes them.

Resolution flow: ``ensure_published_image(preset, arch, vmm)`` looks up the
matching entry, converts it to an :class:`ImageSource`, delegates to
:class:`ImageManager` for caching + SHA-256-verified download, and
decompresses the rootfs if the URL ends in ``.zst``.

Why ``vmm`` is a separate dimension: the kernel must be tuned for the
hypervisor it runs under (Firecracker uses MMIO virtio + 8250 UART; QEMU
uses PCI virtio + PL011 UART on aarch64). The same rootfs works for both
since it's just a filesystem, but different kernels are required. The
caller (typically the CLI) decides which ``vmm`` to request — this module
is policy-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from smolvm import __version__
from smolvm.exceptions import ImageError
from smolvm.images.manager import ImageManager, ImageSource, LocalImage

Arch = Literal["amd64", "arm64"]
Preset = Literal["codex", "claude-code", "openclaw", "hermes", "pi"]
# ``libkrun`` is reserved here for a future spike — manifest accepts the type
# but the CLI never resolves a host to it until libkrun support is wired.
Vmm = Literal["firecracker", "qemu", "libkrun"]
# Kernel binary format. Firecracker requires "elf" (uncompressed ELF
# vmlinux); QEMU on aarch64 virt only boots "image" (the Linux ARM64 boot
# protocol — bzImage on x86, Image on arm64). Same kernel source build
# produces both as a side effect, so we ship both per arch.
KernelFormat = Literal["elf", "image"]


class PublishedImage(BaseModel):
    """One row of the published-image manifest."""

    preset: Preset
    arch: Arch
    vmm: Vmm
    kernel_url: str
    kernel_sha256: str
    rootfs_url: str
    rootfs_sha256: str

    model_config = {"frozen": True}


class BaseKernel(BaseModel):
    """Per-arch SmolVM-built microvm kernels — two formats from one build.

    The ``kernel/microvm/build.sh`` recipe produces both an ELF and a boot-wrapper
    artifact in a single make invocation. We ship both because:

    - Firecracker requires the ELF (``elf_url``) — its kernel loader rejects
      bzImage/Image with ``Invalid Elf magic number``.
    - QEMU on aarch64 ``virt`` empirically refuses to boot a Linux ELF
      vmlinux (silent hang, no console output). It boots Image fine.
      We standardise on the boot-wrapper format (``image_url``) for QEMU
      on both archs.

    Same kernel sources, same Kconfig, same boot behavior — just different
    container format on the wire. Replaces the previously-fetched
    Firecracker-CI kernel (S3) and Ubuntu cloud-image kernel.
    """

    arch: Arch
    elf_url: str
    elf_sha256: str
    image_url: str
    image_sha256: str

    model_config = {"frozen": True}

    def url_for(self, fmt: KernelFormat) -> str:
        return self.elf_url if fmt == "elf" else self.image_url

    def sha256_for(self, fmt: KernelFormat) -> str:
        return self.elf_sha256 if fmt == "elf" else self.image_sha256


def release_tag(version: str = __version__) -> str:
    """GitHub Releases tag images for ``version`` are published under."""
    return f"images-v{version}"


def cache_name(preset: Preset, arch: Arch, vmm: Vmm, version: str = __version__) -> str:
    """Cache directory name under ``~/.smolvm/images/``.

    Versioned + arch- + vmm-suffixed so multiple installs, architectures,
    and hypervisors coexist on the same machine without overwriting each
    other. A user switching backends mid-session gets a fresh cache dir
    per vmm rather than fighting over one.

    Note: caches from before the vmm dimension landed (no ``-<vmm>``
    suffix) become orphaned and ignored. They stay on disk untouched
    until the user clears them.
    """
    return f"{preset}-v{version}-{arch}-{vmm}"


def _release_asset_url(preset: Preset, arch: Arch, suffix: str, version: str) -> str:
    """Construct the post-publish GH Releases asset URL for one artifact.

    Once the draft release at ``images-v<version>`` is published, GH
    Releases serves assets at this canonical URL (the draft itself uses
    a temporary ``untagged-*`` slug — these URLs only resolve after
    the draft is published manually).
    """
    return (
        f"https://github.com/CelestoAI/SmolVM/releases/download/"
        f"{release_tag(version)}/{preset}-{arch}-{suffix}"
    )


def _release_kernel_url(arch: Arch, fmt: KernelFormat, version: str) -> str:
    """URL for a preset-independent kernel artifact.

    Asset naming: ``vmlinux-<arch>.{elf|image}``. Same Linux source build
    produces both formats (see ``kernel/microvm/build.sh``); the runtime picks
    by backend (Firecracker→elf, QEMU→image).
    """
    return (
        f"https://github.com/CelestoAI/SmolVM/releases/download/"
        f"{release_tag(version)}/vmlinux-{arch}.{fmt}"
    )


# Version of the published images this CLI release was paired with.
# Bumping this requires regenerating every MANIFEST entry below from a
# fresh CI run (new artifacts → new SHAs → new URLs).
_MANIFEST_VERSION = "0.0.14a0"

# Per-arch SmolVM-built kernels. Each :class:`BaseKernel` carries BOTH the
# ELF (Firecracker) and Image (QEMU) URLs+SHAs from the same source build.
# Source of truth for kernel URL+SHA: the MANIFEST rows below reference
# these entries via ``url_for(format)`` / ``sha256_for(format)``, so adding
# a future preset can't drift kernel SHAs across rows.
#
# SHAs come from the build-microvm-kernel.yml CI run that publishes
# ``vmlinux-<arch>.{elf,image}`` to release tag ``images-v<_MANIFEST_VERSION>``;
# they're also visible in that workflow's step summary.
BASE_KERNELS: dict[Arch, BaseKernel] = {
    "amd64": BaseKernel(
        arch="amd64",
        elf_url=_release_kernel_url("amd64", "elf", _MANIFEST_VERSION),
        elf_sha256="f652d798efb2b19c4923e5e7ff4e7b2e9db31ec8347cec2a5e6a27b813b2d5a1",
        image_url=_release_kernel_url("amd64", "image", _MANIFEST_VERSION),
        image_sha256="55061bc45706eca229afdad31451d63e0695ce990fb1d46301194b4771e607f0",
    ),
    "arm64": BaseKernel(
        arch="arm64",
        elf_url=_release_kernel_url("arm64", "elf", _MANIFEST_VERSION),
        elf_sha256="d837ec0f6c12d6dd9b96885464db876699e3984c9c8b0c3699569e2000221fc2",
        image_url=_release_kernel_url("arm64", "image", _MANIFEST_VERSION),
        image_sha256="200862461ac269baf56c636a76a96db204e216fc8d16a983b910cd22bf72469b",
    ),
}


def _kernel_format_for_vmm(vmm: Vmm) -> KernelFormat:
    """Map runtime vmm to the kernel binary format it accepts.

    Firecracker requires an uncompressed ELF; QEMU on aarch64 ``virt`` only
    boots the Linux ARM64 boot-protocol Image (silent hang on ELF). libkrun
    is Firecracker-API-compatible and uses the same ELF.
    """
    return "elf" if vmm in {"firecracker", "libkrun"} else "image"


# Bundled preset manifest. The kernel URL/SHA on each row mirrors the matching
# BASE_KERNELS entry's format-for-vmm so a single CI run is the source of
# truth for every kernel SHA. Rootfs SHAs come from build-published-images.yml
# CI for the matching tag.
def _manifest_row(preset: Preset, arch: Arch, vmm: Vmm, rootfs_sha256: str) -> PublishedImage:
    base = BASE_KERNELS[arch]
    fmt = _kernel_format_for_vmm(vmm)
    return PublishedImage(
        preset=preset,
        arch=arch,
        vmm=vmm,
        kernel_url=base.url_for(fmt),
        kernel_sha256=base.sha256_for(fmt),
        rootfs_url=_release_asset_url(preset, arch, "rootfs.ext4.zst", _MANIFEST_VERSION),
        rootfs_sha256=rootfs_sha256,
    )


_OPENCLAW_AMD64_ROOTFS_SHA = "a332941df1dfe3d29c849072267148d84919e6b13fa810870049a0a3567be8f9"
_OPENCLAW_ARM64_ROOTFS_SHA = "87a8d855801a1dc89bafa4ac9596767a5de221536a5f6a43bc57e308059af937"


def _preset_rows(
    preset: Preset, amd64_sha: str, arm64_sha: str
) -> dict[tuple[Preset, Arch, Vmm], PublishedImage]:
    """Generate all (preset, arch, vmm) manifest rows for a single preset."""
    vmms: tuple[Vmm, ...] = ("firecracker", "qemu", "libkrun")
    archs: tuple[Arch, ...] = ("amd64", "arm64")
    shas: dict[Arch, str] = {"amd64": amd64_sha, "arm64": arm64_sha}
    rows: dict[tuple[Preset, Arch, Vmm], PublishedImage] = {}
    for arch in archs:
        for vmm in vmms:
            rows[(preset, arch, vmm)] = _manifest_row(preset, arch, vmm, shas[arch])
    return rows


MANIFEST: dict[tuple[Preset, Arch, Vmm], PublishedImage] = {
    **_preset_rows("openclaw", _OPENCLAW_AMD64_ROOTFS_SHA, _OPENCLAW_ARM64_ROOTFS_SHA),
    # codex, claude-code, hermes, pi: rows added after their first CI build
    # populates real SHAs. Until then these presets fall through to
    # install-at-boot via is_preset_published() returning False.
}


def lookup(
    preset: Preset,
    arch: Arch,
    vmm: Vmm,
    *,
    manifest: dict[tuple[Preset, Arch, Vmm], PublishedImage] | None = None,
) -> PublishedImage:
    """Look up a manifest entry, raising :class:`ImageError` if missing."""
    catalog = MANIFEST if manifest is None else manifest
    entry = catalog.get((preset, arch, vmm))
    if entry is None:
        available = ", ".join(sorted(f"{p}/{a}/{v}" for (p, a, v) in catalog)) or "(none)"
        raise ImageError(
            f"No published image for preset '{preset}' on arch '{arch}' under "
            f"vmm '{vmm}' (available: {available})."
        )
    return entry


def is_preset_published(
    preset: str,
    arch: Arch,
    vmm: Vmm,
    *,
    manifest: dict[tuple[Preset, Arch, Vmm], PublishedImage] | None = None,
) -> bool:
    """Return whether a published image is registered for ``(preset, arch, vmm)``.

    Used by the CLI dispatch to decide whether to take the fast published-image
    path or fall back to install-at-boot. Accepts an arbitrary preset string so
    callers don't need to coerce against the ``Preset`` literal — presets that
    don't appear in the manifest just return ``False``.
    """
    catalog = MANIFEST if manifest is None else manifest
    return (preset, arch, vmm) in catalog  # type: ignore[comparison-overlap]


def to_image_source(entry: PublishedImage, version: str = __version__) -> ImageSource:
    """Convert a manifest entry into an :class:`ImageSource` for the manager.

    When the rootfs URL is zstd-compressed (``*.zst``), the cache filename
    keeps the ``.zst`` suffix so :class:`ImageManager` verifies the SHA-256
    of the compressed bytes that ship over the wire. ``ensure_published_image``
    decompresses alongside afterward.
    """
    rootfs_filename = "rootfs.ext4.zst" if entry.rootfs_url.endswith(".zst") else "rootfs.ext4"
    return ImageSource(
        name=cache_name(entry.preset, entry.arch, entry.vmm, version),
        kernel_url=entry.kernel_url,
        kernel_sha256=entry.kernel_sha256,
        rootfs_url=entry.rootfs_url,
        rootfs_sha256=entry.rootfs_sha256,
        rootfs_filename=rootfs_filename,
    )


def _decompress_zstd(src: Path, dst: Path) -> None:
    """Stream-decompress a zstd file. Writes to a sibling ``.tmp`` then renames."""
    import zstandard

    tmp = dst.parent / (dst.name + ".tmp")
    try:
        with src.open("rb") as src_f, tmp.open("wb") as dst_f:
            zstandard.ZstdDecompressor().copy_stream(src_f, dst_f)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_published_image(
    preset: Preset,
    arch: Arch,
    vmm: Vmm,
    *,
    cache_dir: Path | None = None,
    manifest: dict[tuple[Preset, Arch, Vmm], PublishedImage] | None = None,
    version: str = __version__,
) -> LocalImage:
    """Download (if needed) and return paths to a published preset image.

    Args:
        preset: Preset name (e.g. ``"codex"``).
        arch: Guest CPU architecture (``"amd64"`` or ``"arm64"``).
        vmm: Hypervisor variant the image was built for (``"firecracker"``,
            ``"qemu"``, or ``"libkrun"``). Caller must pre-resolve this from
            host platform — this module is policy-free.
        cache_dir: Override the default cache directory (mainly for tests).
        manifest: Override the bundled manifest (mainly for tests).
        version: Override the CLI version used to compute the cache name.

    Returns:
        :class:`LocalImage` with paths to the kernel and (decompressed)
        rootfs. When the manifest entry's rootfs URL is zstd-compressed,
        the compressed file stays in cache (so SHA verification can re-run
        on subsequent calls) and a sibling decompressed ``rootfs.ext4`` is
        produced lazily.

    Raises:
        ImageError: If no manifest entry exists for the (preset, arch, vmm)
            tuple, or if download / SHA-256 verification fails.
    """
    entry = lookup(preset, arch, vmm, manifest=manifest)
    source = to_image_source(entry, version=version)
    manager = ImageManager(cache_dir=cache_dir, registry={source.name: source})
    local = manager.ensure_image(source.name)

    # Wire-format short-circuit: nothing to decompress.
    if not source.rootfs_filename.endswith(".zst"):
        return local

    # Decompress alongside. We invalidate the decompressed file when the
    # source ``.zst`` SHA changes (e.g. between a manifest version bump or
    # an in-place rootfs republish) by stamping the source SHA into a
    # sidecar file. Without this, a refreshed ``.zst`` was silently served
    # alongside a stale ``.ext4`` from the previous SHA — first surfaced
    # while diagnosing the openclaw uid bake regression.
    decompressed_path = local.rootfs_path.with_suffix("")
    sidecar_path = decompressed_path.with_name(decompressed_path.name + ".from-sha256")
    expected_sha = entry.rootfs_sha256
    sidecar_sha = sidecar_path.read_text().strip() if sidecar_path.is_file() else None
    if not decompressed_path.is_file() or sidecar_sha != expected_sha:
        _decompress_zstd(local.rootfs_path, decompressed_path)
        sidecar_path.write_text(expected_sha)

    return local.model_copy(update={"rootfs_path": decompressed_path})


def ensure_base_kernel(
    arch: Arch,
    fmt: KernelFormat,
    *,
    cache_dir: Path | None = None,
    registry: dict[Arch, BaseKernel] | None = None,
    version: str = __version__,
) -> Path:
    """Download (if needed) and return the local path to the base kernel.

    The base kernel exists in two formats per arch (see :class:`BaseKernel`):
    ``elf`` for Firecracker (and libkrun), ``image`` for QEMU. Same source
    build, different container formats. All consumers — the auto-config
    flow, the preset image builder, the published-image launcher — funnel
    through this function so there is exactly one kernel binary cached on
    disk per ``(arch, fmt)``.

    Args:
        arch: Guest CPU architecture.
        fmt: Binary format the runtime expects (``"elf"`` for Firecracker,
            ``"image"`` for QEMU).
        cache_dir: Override the default cache directory (mainly for tests).
        registry: Override :data:`BASE_KERNELS` (mainly for tests).
        version: Override the CLI version used to compute the cache name.

    Returns:
        Absolute path to the cached, SHA-256-verified vmlinux file.

    Raises:
        ImageError: If no base kernel is registered for ``arch``, or if
            download / SHA verification fails.
    """
    catalog = BASE_KERNELS if registry is None else registry
    entry = catalog.get(arch)
    if entry is None:
        available = ", ".join(sorted(catalog)) or "(none)"
        raise ImageError(
            f"No base kernel registered for arch '{arch}' (available: {available})."
        )
    manager = ImageManager(cache_dir=cache_dir)
    # Cache filename keeps the format suffix so the elf and image artifacts
    # don't collide for the same (version, arch). Same dir is fine — both
    # files coexist when a user runs both backends.
    return manager.ensure_rootfs_only(
        name=f"base-kernel-v{version}-{arch}",
        url=entry.url_for(fmt),
        filename=f"vmlinux.{fmt}",
        sha256=entry.sha256_for(fmt),
    )
