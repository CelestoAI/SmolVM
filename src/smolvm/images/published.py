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
Preset = Literal["codex", "claude-code", "openclaw", "hermes"]
# ``libkrun`` is reserved here for a future spike — manifest accepts the type
# but the CLI never resolves a host to it until libkrun support is wired.
Vmm = Literal["firecracker", "qemu", "libkrun"]


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


def _release_kernel_url(arch: Arch, vmm: Vmm, version: str) -> str:
    """URL for a preset-independent kernel artifact.

    QEMU/libkrun kernels are built once per (arch, vmm) and shared across
    all presets — the kernel doesn't depend on the userspace baked into
    the rootfs. The asset name reflects that: ``vmlinux-<arch>-<vmm>.bin``,
    no preset prefix. (The older Firecracker assets predate this convention
    and use ``<preset>-<arch>-vmlinux.bin``; renaming them is a future
    cleanup task.)
    """
    return (
        f"https://github.com/CelestoAI/SmolVM/releases/download/"
        f"{release_tag(version)}/vmlinux-{arch}-{vmm}.bin"
    )


# Version of the published images this CLI release was paired with.
# Bumping this requires regenerating every MANIFEST entry below from a
# fresh CI run (new artifacts → new SHAs → new URLs). The drift-detection
# test in test_published_images.py asserts this matches __version__ so
# pyproject.toml version bumps don't ship with stale manifest entries.
_MANIFEST_VERSION = "0.0.13"

# Bundled manifest. New (preset, arch, vmm) entries land here as CI publishes
# images — paired by version with this CLI release. The SHA-256s and URLs
# below match the artifacts produced by the CI workflow at
# .github/workflows/build-published-images.yml; they're also visible in
# the corresponding GH release's step summary on each successful run.
MANIFEST: dict[tuple[Preset, Arch, Vmm], PublishedImage] = {
    ("openclaw", "amd64", "firecracker"): PublishedImage(
        preset="openclaw",
        arch="amd64",
        vmm="firecracker",
        kernel_url=_release_asset_url("openclaw", "amd64", "vmlinux.bin", _MANIFEST_VERSION),
        kernel_sha256="d361a5f2e67b2e243964ad93f25a2d9e5bee320204a84a7af089949228af5c2a",
        rootfs_url=_release_asset_url("openclaw", "amd64", "rootfs.ext4.zst", _MANIFEST_VERSION),
        rootfs_sha256="919eea4fdaae8674c6749cb6acd9a1b51235369e412a08d38c5062519eaf8875",
    ),
    ("openclaw", "arm64", "firecracker"): PublishedImage(
        preset="openclaw",
        arch="arm64",
        vmm="firecracker",
        kernel_url=_release_asset_url("openclaw", "arm64", "vmlinux.bin", _MANIFEST_VERSION),
        kernel_sha256="7d8dc0bce701037ea5ceccfc997c05b11f99aba215c73ed18a2269154837c497",
        rootfs_url=_release_asset_url("openclaw", "arm64", "rootfs.ext4.zst", _MANIFEST_VERSION),
        rootfs_sha256="bb5c42ffdd757ffb48c62f9d841230ff4e345a75fc40df210722d0a598626f29",
    ),
    # QEMU rows reuse the firecracker rootfs (same userspace; only the
    # kernel differs). Kernels come from the SmolVM-built QEMU/libkrun
    # kernel workflow (.github/workflows/build-qemu-kernel.yml) and use
    # the preset-independent naming `vmlinux-<arch>-qemu.bin`.
    ("openclaw", "amd64", "qemu"): PublishedImage(
        preset="openclaw",
        arch="amd64",
        vmm="qemu",
        kernel_url=_release_kernel_url("amd64", "qemu", _MANIFEST_VERSION),
        kernel_sha256="db6ddc88e5b88941164df53f5f798d080b95a90c411df8d2b9f501eb18fb89aa",
        rootfs_url=_release_asset_url("openclaw", "amd64", "rootfs.ext4.zst", _MANIFEST_VERSION),
        rootfs_sha256="919eea4fdaae8674c6749cb6acd9a1b51235369e412a08d38c5062519eaf8875",
    ),
    ("openclaw", "arm64", "qemu"): PublishedImage(
        preset="openclaw",
        arch="arm64",
        vmm="qemu",
        kernel_url=_release_kernel_url("arm64", "qemu", _MANIFEST_VERSION),
        kernel_sha256="6f4f42cfa1d3038bd06d99e09887119e4c7fdd2d1da02913e1b6b10359376752",
        rootfs_url=_release_asset_url("openclaw", "arm64", "rootfs.ext4.zst", _MANIFEST_VERSION),
        rootfs_sha256="bb5c42ffdd757ffb48c62f9d841230ff4e345a75fc40df210722d0a598626f29",
    ),
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

    # Decompress alongside, only on cache miss for the decompressed file.
    decompressed_path = local.rootfs_path.with_suffix("")
    if not decompressed_path.is_file():
        _decompress_zstd(local.rootfs_path, decompressed_path)

    return local.model_copy(update={"rootfs_path": decompressed_path})
