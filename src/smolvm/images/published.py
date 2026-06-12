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
to a content release tag such as ``images-2026.06.12.0``. This tag is
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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from smolvm import __version__
from smolvm.exceptions import ImageError
from smolvm.images.manager import ImageManager, ImageSource, LocalImage

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
#   1. Edit this CalVer string (e.g. ``images-2026.06.12.0``).
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
IMAGES_RELEASE_TAG = "images-2026.06.12.0"


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
        elf_sha256="57db0d8ae115aeb6d5d2ca599aceae3251c57b62dc62c1c9fe0bd8ecb24ee553",
        image_url=_release_kernel_url("amd64", "image"),
        image_sha256="cd9e911f4e07a116b500331237a55d6c877d645b1a7eaf7a279be04a7f801437",
    ),
    "arm64": BaseKernel(
        arch="arm64",
        elf_url=_release_kernel_url("arm64", "elf"),
        elf_sha256="537058a71feb49b2e27742e1908038a92e98efc3cb7161fa477611c351c0208c",
        image_url=_release_kernel_url("arm64", "image"),
        image_sha256="f20ae22346d9fe2f68749456b156c91eb21c21859562cd62592dd8d04a4b80bf",
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
_OPENCLAW_AMD64_ROOTFS_SHA = "4524d4c1f8c1c3bfeea521ae0af595fcb1df6d24eea4f9087f5656682049b927"
_OPENCLAW_ARM64_ROOTFS_SHA = "c3ae4380733263390c0c7630353c2a1cf417a71c3a866705bff48cb3b0c87b38"
_CODEX_AMD64_ROOTFS_SHA = "dbd1b02a96989dba161d49d34f2eb6b78ffdc612622b2932c2715cc7ff12c7dd"
_CODEX_ARM64_ROOTFS_SHA = "8b0cff275c837bb1c7a8959f23b90c53ff3b09d8eb18650372c720ab03dd5903"
_CLAUDE_CODE_AMD64_ROOTFS_SHA = "261d283e6025b76ae26ad73a4870b43cb0f5b206c58e2641a33b9b17095b329e"
_CLAUDE_CODE_ARM64_ROOTFS_SHA = "c54c3b5ec866eff1a9360f1f55414847a029f179777d2bbd52e7b3e801affcfb"
_HERMES_AMD64_ROOTFS_SHA = "4dd7b27139bf359f452c447518aac9c9dea712c8122e93636df4c7594c1b13be"
_HERMES_ARM64_ROOTFS_SHA = "fb0174fa22248b2abc50cb4cf0537087bcfb51f78c17fe4b57d2f7c96c076d4c"
_PI_AMD64_ROOTFS_SHA = "e45dd13c08c4b0a19e2663e8dcf88be2a8958ec4e1afebc9d181402547d02750"
_PI_ARM64_ROOTFS_SHA = "2162ce84a78dd9d4cd153e81f43b154f5ebb9caaa497a9d20014aafcdf65fd69"
# Bare Ubuntu base image (no preset install) — raw-ext4, agent baked in.
_UBUNTU_AMD64_ROOTFS_SHA = "090ff2345bc36bebcc37352e293fe9a847ed7a1dc306f0e9bae3b2119a939626"
_UBUNTU_ARM64_ROOTFS_SHA = "ef8f7792cd85355384fc07e21b6f899a91c8cabc37d160a4b51c52286b9d2e28"


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


# Alpine rootfs SHAs are populated after the first successful run of
# ``build-published-images.yml`` against this :data:`IMAGES_RELEASE_TAG`.
# Until then the Alpine ``_preset_rows`` calls below stay commented so the
# manifest doesn't reference URLs whose SHA we can't verify. Phase 1 of
# #264 covers codex/claude-code/pi only — hermes is excluded in CI for
# musllinux compatibility reasons; openclaw uses a separate builder.

MANIFEST: dict[ManifestKey, PublishedImage] = {
    # Ubuntu rows — historical default, no naming change in URL.
    **_preset_rows("openclaw", _OPENCLAW_AMD64_ROOTFS_SHA, _OPENCLAW_ARM64_ROOTFS_SHA),
    **_preset_rows("codex", _CODEX_AMD64_ROOTFS_SHA, _CODEX_ARM64_ROOTFS_SHA),
    **_preset_rows("claude-code", _CLAUDE_CODE_AMD64_ROOTFS_SHA, _CLAUDE_CODE_ARM64_ROOTFS_SHA),
    **_preset_rows("hermes", _HERMES_AMD64_ROOTFS_SHA, _HERMES_ARM64_ROOTFS_SHA),
    **_preset_rows("pi", _PI_AMD64_ROOTFS_SHA, _PI_ARM64_ROOTFS_SHA),
    # Bare Ubuntu base image (raw-ext4, agent baked in) — powers
    # ``create --os ubuntu`` on firecracker/libkrun (the qemu path keeps its
    # qcow2 cloud image). Same rootfs shared across vmms; only the kernel differs.
    **_preset_rows("ubuntu", _UBUNTU_AMD64_ROOTFS_SHA, _UBUNTU_ARM64_ROOTFS_SHA),
    # Alpine rows — add once CI publishes ``<preset>-<arch>-alpine-rootfs.ext4.zst``
    # under :data:`IMAGES_RELEASE_TAG` and we have SHAs to pin against.
    # Until then, ``smolvm <preset> start --os alpine`` falls through to
    # install-at-boot via the local Docker builder.
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
    os: Os = "ubuntu",
    *,
    cache_dir: Path | None = None,
    manifest: dict[ManifestKey, PublishedImage] | None = None,
    version: str = __version__,
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
