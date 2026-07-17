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
to a content release tag such as ``images-2026.06.14.0``. This tag is
intentionally independent of the SmolVM package version because image and
rootfs rebuilds have their own cadence. The ``MANIFEST`` below is the
bundled catalog the CLI ships with — entries are appended as CI publishes
them.

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

import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from smolvm import __version__
from smolvm.exceptions import ImageError
from smolvm.images.manager import ImageManager, ImageSource, LocalImage
from uuid import uuid4

Arch = Literal["amd64", "arm64"]
Preset = Literal["codex", "claude-code", "openclaw", "hermes", "pi", "ubuntu"]
# ``libkrun`` is reserved here for a future spike — manifest accepts the type
# but the CLI never resolves a host to it until libkrun support is wired.
Vmm = Literal["firecracker", "qemu", "libkrun"]
# Guest OS the rootfs was built from. Ubuntu 24.04 is the historical default;
# Alpine 3.20 is the smaller-footprint opt-in (~half the size, faster boot)
# rolled out on a per-preset basis — see issue #264.
Os = Literal["ubuntu", "alpine"]
# Kernel binary format. Firecracker requires "elf" (uncompressed ELF
# vmlinux); QEMU on aarch64 virt only boots "image" (the Linux ARM64 boot
# protocol — bzImage on x86, Image on arm64). Same kernel source build
# produces both as a side effect, so we ship both per arch.
KernelFormat = Literal["elf", "image"]

# Manifest key: ``(preset, arch, vmm, os)``. The OS dimension was added in
# the Alpine rollout — same kernel boots both flavours, only the rootfs
# differs, so no extra kernel artifacts ship per OS.
ManifestKey = tuple[Preset, Arch, Vmm, Os]


class PublishedImage(BaseModel):
    """One row of the published-image manifest."""

    preset: Preset
    arch: Arch
    vmm: Vmm
    os: Os
    kernel_url: str
    kernel_sha256: str
    rootfs_url: str
    rootfs_sha256: str

    # ``extra="forbid"`` rejects unknown fields at construction so a typo
    # in a manifest row (e.g. ``kernel_shaa256=...``) fails loudly instead
    # of being silently ignored and shipping an unverified image.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


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

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def url_for(self, fmt: KernelFormat) -> str:
        return self.elf_url if fmt == "elf" else self.image_url

    def sha256_for(self, fmt: KernelFormat) -> str:
        return self.elf_sha256 if fmt == "elf" else self.image_sha256


# SINGLE SOURCE OF TRUTH for the GitHub Releases tag this CLI pulls
# images from. Independent of the CLI version in ``pyproject.toml`` —
# coupling them (via the previous ``_MANIFEST_VERSION``) meant a CLI
# bump silently invalidated every manifest SHA pin.
#
# CI workflows (``build-microvm-kernel.yml``,
# ``build-published-images.yml``) read this constant rather than
# deriving from pyproject — change here flows to both sides.
#
# Bumping protocol — one PR:
#   1. Edit this CalVer string (e.g. ``images-2026.06.14.0``).
#      Bump the final sequence when publishing more than once on a day.
#   2. Run the kernel + published-image workflows, copy SHAs from the
#      step summaries into ``BASE_KERNELS`` and ``MANIFEST``.
#   3. Promote the draft release on GitHub.
#
# CI no longer passes ``--clobber`` by default (see ``ci`` PR #280) — a
# bump that forgets to land its SHA-resync commit will fail loud at the
# upload step instead of silently swapping bytes under the existing
# pins. Re-bakes against the same tag must opt in via the
# ``force_overwrite`` workflow_dispatch input.
IMAGES_RELEASE_TAG = "images-2026.06.30.0"


def _images_release_tag() -> str:
    """Return the image release tag used when constructing download URLs.

    ``IMAGES_RELEASE_TAG`` remains the public source of truth. The environment
    override is for CI jobs that need to test a branch before the bumped image
    release has been published.
    """
    return os.environ.get("SMOLVM_IMAGES_RELEASE_TAG") or IMAGES_RELEASE_TAG


def cache_name(
    preset: Preset, arch: Arch, vmm: Vmm, version: str = __version__, *, os: Os = "ubuntu"
) -> str:
    """Cache directory name under ``~/.smolvm/images/``.

    Keyed on the **CLI** version (not the images tag) so a CLI upgrade
    invalidates local caches even when the images tag hasn't moved —
    avoids serving a stale ``.ext4`` decompressed from the prior CLI's
    decompression sidecar.

    OS suffix is omitted for Ubuntu so existing on-disk caches from before
    the OS dimension was added stay valid; Alpine adds an explicit suffix.
    ``os`` is keyword-only so existing positional callers (with ``version``
    as the 4th argument) keep working.
    """
    base = f"{preset}-v{version}-{arch}-{vmm}"
    return base if os == "ubuntu" else f"{base}-{os}"


def _release_asset_url(preset: Preset, arch: Arch, suffix: str, os: Os = "ubuntu") -> str:
    """Construct the post-publish GH Releases asset URL for one artifact.

    Once the draft at :data:`IMAGES_RELEASE_TAG` is published, GH Releases
    serves assets at this canonical URL (drafts use a temporary
    ``untagged-*`` slug — these URLs only resolve after publish).

    Naming asymmetry: Ubuntu assets keep the ``{preset}-{arch}-{suffix}``
    shape they had before the OS dimension landed, so the existing release
    artifacts stay reachable. Alpine assets carry an explicit ``-alpine-``
    segment. ``scripts/ci/build-preset.sh`` mirrors this on the upload side.
    """
    slug = f"{preset}-{arch}-{suffix}" if os == "ubuntu" else f"{preset}-{arch}-{os}-{suffix}"
    return f"https://github.com/CelestoAI/SmolVM/releases/download/{_images_release_tag()}/{slug}"


def _release_kernel_url(arch: Arch, fmt: KernelFormat) -> str:
    """URL for a preset-independent kernel artifact.

    Asset naming: ``vmlinux-<arch>.{elf|image}``. Same Linux source build
    produces both formats (see ``kernel/microvm/build.sh``); the runtime picks
    by backend (Firecracker→elf, QEMU→image).
    """
    return (
        f"https://github.com/CelestoAI/SmolVM/releases/download/"
        f"{_images_release_tag()}/vmlinux-{arch}.{fmt}"
    )


# Per-arch SmolVM-built kernels. Each :class:`BaseKernel` carries BOTH the
# ELF (Firecracker) and Image (QEMU) URLs+SHAs from the same source build.
# Source of truth for kernel URL+SHA: the MANIFEST rows below reference
# these entries via ``url_for(format)`` / ``sha256_for(format)``, so adding
# a future preset can't drift kernel SHAs across rows.
#
# SHAs come from the ``Build microvm Kernel`` workflow run that publishes
# ``vmlinux-<arch>.{elf,image}`` to :data:`IMAGES_RELEASE_TAG`; they're
# also visible in that workflow's step summary.
BASE_KERNELS: dict[Arch, BaseKernel] = {
    "amd64": BaseKernel(
        arch="amd64",
        elf_url=_release_kernel_url("amd64", "elf"),
        elf_sha256="d39c47b5f1d9bc797871d4f120e4496ad11e0540e3a6bcf1a4544b1640eee014",
        image_url=_release_kernel_url("amd64", "image"),
        image_sha256="a7a8da6ad55236edccbdd1015eda023d68a4879be648a348462119138de54cb3",
    ),
    "arm64": BaseKernel(
        arch="arm64",
        elf_url=_release_kernel_url("arm64", "elf"),
        elf_sha256="d6e8c9a0e1123691fe9cb648b275cca7dbe176798b033ddb45405209d42c32d3",
        image_url=_release_kernel_url("arm64", "image"),
        image_sha256="99e2f80dc54653bd7d5f74a59c8c5c765fea52f23e664ca50021782da5f3fa7f",
    ),
}


def _kernel_format_for_vmm(vmm: Vmm) -> KernelFormat:
    # libkrun on Linux (KVM) accepts ELF; on macOS (Hypervisor.framework) it
    # requires the ARM64 Image format — same as QEMU.
    import platform

    if vmm == "libkrun" and platform.system() == "Darwin":
        return "image"
    return "elf" if vmm in {"firecracker", "libkrun"} else "image"


# Bundled preset manifest. The kernel URL/SHA on each row mirrors the matching
# BASE_KERNELS entry's format-for-vmm so a single CI run is the source of
# truth for every kernel SHA. Rootfs SHAs come from build-published-images.yml
# CI for the matching tag.
def _manifest_row(
    preset: Preset, arch: Arch, vmm: Vmm, os: Os, rootfs_sha256: str
) -> PublishedImage:
    base = BASE_KERNELS[arch]
    fmt = _kernel_format_for_vmm(vmm)
    return PublishedImage(
        preset=preset,
        arch=arch,
        vmm=vmm,
        os=os,
        kernel_url=base.url_for(fmt),
        kernel_sha256=base.sha256_for(fmt),
        rootfs_url=_release_asset_url(preset, arch, "rootfs.ext4.zst", os=os),
        rootfs_sha256=rootfs_sha256,
    )


# Rootfs SHAs from the Build Published Images run for IMAGES_RELEASE_TAG.
# Captured by sha256sum'ing each ``<preset>-<arch>-rootfs.ext4.zst`` asset
# on the release page. Update together when artifacts are rebuilt.
_OPENCLAW_AMD64_ROOTFS_SHA = "5de5afa8b089ca2e19e6f7a3e2069d5a51483188a7224a6dcac9f05bfa4759ba"
_OPENCLAW_ARM64_ROOTFS_SHA = "442b5e08df0d3ade722942ba3aa61b9eb595910ad08aa85ecc7ad6d153daa33e"
_CODEX_AMD64_ROOTFS_SHA = "3af55cf86c1f5bf2521141968600b1922b9cd26c8a5545e93c4099c84a871f7c"
_CODEX_ARM64_ROOTFS_SHA = "03e62729008cc2b530e5dfd03e40f78b10282434d8a892e16b83808d944cec1a"
_CLAUDE_CODE_AMD64_ROOTFS_SHA = "86fc1641972094c78c8313fff0c84004d2656c1685022e91ec558fc4066c6d36"
_CLAUDE_CODE_ARM64_ROOTFS_SHA = "acc97704cf524ed39ef65953bec2fe1c7aea899d9542a5abacda8448f170e681"
_HERMES_AMD64_ROOTFS_SHA = "3456e9cb17f85cd5c71aa4db85de91d8b03fdebb9b15247eb7024b318bfa7348"
_HERMES_ARM64_ROOTFS_SHA = "95d6dec3d79ef9f8f96214691586e99511916bad0c4e9b7b528cd6c4b5fa43f9"
_PI_AMD64_ROOTFS_SHA = "f0c2b2e59c270cfd3a4412145782fd0eb5d9e92d9c5f03b073e5e2d84076246e"
_PI_ARM64_ROOTFS_SHA = "31275d5b8ca37d09f1e1836f67b1cfe5d223902d8350f02b0ccc7fd74d9045b7"
# Bare Ubuntu base image (no preset install) — raw-ext4, agent baked in.
_UBUNTU_AMD64_ROOTFS_SHA = "ec592449bf2511ffdf675e6fc0f50ae68e3f5fc90b3eb4eed7638ddf6a6642b3"
_UBUNTU_ARM64_ROOTFS_SHA = "73236b135d1882e576bc01cbe529ada783e1c5f03aee0ab7959e6ddaf026e349"
_CODEX_AMD64_ALPINE_ROOTFS_SHA = "4a93881f16bd38b1aea1cb9be75c7ff1b7e35365fe8123a2b67c4bf7e635ab3e"
_CODEX_ARM64_ALPINE_ROOTFS_SHA = "d2db1e44899fa5073dd34b9cbaea66156da09400893c55fae79a4f51eda56141"
_CLAUDE_CODE_AMD64_ALPINE_ROOTFS_SHA = (
    "c9897a45dcbf111ed9af9f3082c0f57483848f94117836ad70b7bf88b0451eee"
)
_CLAUDE_CODE_ARM64_ALPINE_ROOTFS_SHA = (
    "d58ff31a5347cee37152ff71def4bdad2e95a4052b234c4d70c4864157b8dd23"
)
_PI_AMD64_ALPINE_ROOTFS_SHA = "663411193e9792f3cb5fce88a1b55f7ca12064021d58b4d5b0ed274d2169c460"
_PI_ARM64_ALPINE_ROOTFS_SHA = "b799cf4057ca96d3640efb75c5b3726fd1e46e6c83afa2420c2b78d503c104c0"


def _preset_rows(
    preset: Preset, amd64_sha: str, arm64_sha: str, *, os: Os = "ubuntu"
) -> dict[ManifestKey, PublishedImage]:
    """Generate all (preset, arch, vmm) manifest rows for a single preset.

    Each call produces 6 rows (2 archs × 3 vmms) for one OS flavour.
    Adding an Alpine variant for a preset means a second call with
    ``os="alpine"`` and the matching SHAs from CI.
    """
    vmms: tuple[Vmm, ...] = ("firecracker", "qemu", "libkrun")
    archs: tuple[Arch, ...] = ("amd64", "arm64")
    shas: dict[Arch, str] = {"amd64": amd64_sha, "arm64": arm64_sha}
    rows: dict[ManifestKey, PublishedImage] = {}
    for arch in archs:
        for vmm in vmms:
            rows[(preset, arch, vmm, os)] = _manifest_row(preset, arch, vmm, os, shas[arch])
    return rows


# Alpine rows are published for codex/claude-code/pi in this release. Hermes is
# excluded in CI for musllinux compatibility reasons; openclaw uses a separate
# builder.

MANIFEST: dict[ManifestKey, PublishedImage] = {
    # Ubuntu rows — historical default, no naming change in URL.
    **_preset_rows("openclaw", _OPENCLAW_AMD64_ROOTFS_SHA, _OPENCLAW_ARM64_ROOTFS_SHA),
    **_preset_rows("codex", _CODEX_AMD64_ROOTFS_SHA, _CODEX_ARM64_ROOTFS_SHA),
    **_preset_rows("claude-code", _CLAUDE_CODE_AMD64_ROOTFS_SHA, _CLAUDE_CODE_ARM64_ROOTFS_SHA),
    **_preset_rows("hermes", _HERMES_AMD64_ROOTFS_SHA, _HERMES_ARM64_ROOTFS_SHA),
    **_preset_rows("pi", _PI_AMD64_ROOTFS_SHA, _PI_ARM64_ROOTFS_SHA),
    # Bare Ubuntu base image (raw-ext4, agent baked in) — powers
    # ``create --os ubuntu`` on every supported VMM. Same rootfs shared
    # across vmms; only the kernel differs.
    **_preset_rows("ubuntu", _UBUNTU_AMD64_ROOTFS_SHA, _UBUNTU_ARM64_ROOTFS_SHA),
    # Alpine rows — published for the presets whose CI builders support Alpine.
    **_preset_rows(
        "codex",
        _CODEX_AMD64_ALPINE_ROOTFS_SHA,
        _CODEX_ARM64_ALPINE_ROOTFS_SHA,
        os="alpine",
    ),
    **_preset_rows(
        "claude-code",
        _CLAUDE_CODE_AMD64_ALPINE_ROOTFS_SHA,
        _CLAUDE_CODE_ARM64_ALPINE_ROOTFS_SHA,
        os="alpine",
    ),
    **_preset_rows("pi", _PI_AMD64_ALPINE_ROOTFS_SHA, _PI_ARM64_ALPINE_ROOTFS_SHA, os="alpine"),
}


def lookup(
    preset: Preset,
    arch: Arch,
    vmm: Vmm,
    os: Os = "ubuntu",
    *,
    manifest: dict[ManifestKey, PublishedImage] | None = None,
) -> PublishedImage:
    """Look up a manifest entry, raising :class:`ImageError` if missing."""
    catalog = MANIFEST if manifest is None else manifest
    entry = catalog.get((preset, arch, vmm, os))
    if entry is None:
        available = ", ".join(sorted(f"{p}/{a}/{v}/{o}" for (p, a, v, o) in catalog)) or "(none)"
        raise ImageError(
            f"No published image for preset '{preset}' on arch '{arch}' under "
            f"vmm '{vmm}' with os '{os}' (available: {available})."
        )
    return entry


def is_preset_published(
    preset: str,
    arch: Arch,
    vmm: Vmm,
    os: Os = "ubuntu",
    *,
    manifest: dict[ManifestKey, PublishedImage] | None = None,
) -> bool:
    """Return whether a published image is registered for ``(preset, arch, vmm, os)``.

    Used by the CLI dispatch to decide whether to take the fast published-image
    path or fall back to install-at-boot. Accepts an arbitrary preset string so
    callers don't need to coerce against the ``Preset`` literal — presets that
    don't appear in the manifest just return ``False``.
    """
    catalog = MANIFEST if manifest is None else manifest
    return (preset, arch, vmm, os) in catalog  # type: ignore[comparison-overlap]


def to_image_source(entry: PublishedImage, version: str = __version__) -> ImageSource:
    """Convert a manifest entry into an :class:`ImageSource` for the manager.

    When the rootfs URL is zstd-compressed (``*.zst``), the cache filename
    keeps the ``.zst`` suffix so :class:`ImageManager` verifies the SHA-256
    of the compressed bytes that ship over the wire. ``ensure_published_image``
    decompresses alongside afterward.
    """
    rootfs_filename = "rootfs.ext4.zst" if entry.rootfs_url.endswith(".zst") else "rootfs.ext4"
    return ImageSource(
        name=cache_name(entry.preset, entry.arch, entry.vmm, version, os=entry.os),
        kernel_url=entry.kernel_url,
        kernel_sha256=entry.kernel_sha256,
        rootfs_url=entry.rootfs_url,
        rootfs_sha256=entry.rootfs_sha256,
        rootfs_filename=rootfs_filename,
    )


_SPARSE_DECOMPRESS_CHUNK_SIZE = 1024 * 1024
_DECOMPRESSED_ROOTFS_CACHE_VERSION = "sparse-v1"


def _decompressed_rootfs_sidecar_value(rootfs_sha256: str) -> str:
    return f"{_DECOMPRESSED_ROOTFS_CACHE_VERSION}:{rootfs_sha256}"


def _decompress_zstd(src: Path, dst: Path) -> None:
    """Stream-decompress a zstd file. Writes to a sibling ``.tmp`` then renames.

    Published raw ext4 images contain large zeroed regions. Keep those regions
    sparse in the local cache so Firecracker isolated-disk materialization does
    not copy gigabytes of zeros before every boot on filesystems without
    reflink support.
    """
    from smolvm.host.disk import decompress_zstd_sparse

    staging = dst.with_name(f".{dst.name}.{uuid4().hex}.partial")
    try:
        decompress_zstd_sparse(src, staging, chunk_size=_SPARSE_DECOMPRESS_CHUNK_SIZE)
        staging.replace(dst)
    except OSError as exc:
        (staging.parent / (staging.name + ".tmp")).unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
        if _looks_like_zstd_decode_error(exc):
            import zstandard

            raise zstandard.ZstdError(str(exc)) from exc
        raise


def _looks_like_zstd_decode_error(error: OSError) -> bool:
    message = str(error).lower()
    markers = ("zstd", "frame", "checksum", "corrupt")
    return any(marker in message for marker in markers)


def ensure_published_image(
    preset: Preset,
    arch: Arch,
    vmm: Vmm,
    os: Os = "ubuntu",
    *,
    cache_dir: Path | None = None,
    manifest: dict[ManifestKey, PublishedImage] | None = None,
    version: str = __version__,
    on_download: Callable[[str, int, int | None], None] | None = None,
) -> LocalImage:
    """Download (if needed) and return paths to a published preset image.

    Args:
        preset: Preset name (e.g. ``"codex"``).
        arch: Guest CPU architecture (``"amd64"`` or ``"arm64"``).
        vmm: Hypervisor variant the image was built for (``"firecracker"``,
            ``"qemu"``, or ``"libkrun"``). Caller must pre-resolve this from
            host platform — this module is policy-free.
        os: Guest operating system flavour the rootfs was built from
            (``"ubuntu"`` or ``"alpine"``). Defaults to ``"ubuntu"``.
        cache_dir: Override the default cache directory (mainly for tests).
        manifest: Override the bundled manifest (mainly for tests).
        version: Override the CLI version used to compute the cache name.
        on_download: Optional callback invoked as published image assets
            are downloaded.

    Returns:
        :class:`LocalImage` with paths to the kernel and (decompressed)
        rootfs. When the manifest entry's rootfs URL is zstd-compressed,
        the compressed file stays in cache (so SHA verification can re-run
        on subsequent calls) and a sibling decompressed ``rootfs.ext4`` is
        produced lazily.

    Raises:
        ImageError: If no manifest entry exists for the (preset, arch, vmm,
            os) tuple, or if download / SHA-256 verification fails.
    """
    entry = lookup(preset, arch, vmm, os, manifest=manifest)
    source = to_image_source(entry, version=version)
    manager = ImageManager(cache_dir=cache_dir, registry={source.name: source})
    local = manager.ensure_image(source.name, on_download=on_download)

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
    expected_sha = _decompressed_rootfs_sidecar_value(entry.rootfs_sha256)
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
        raise ImageError(f"No base kernel registered for arch '{arch}' (available: {available}).")
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
