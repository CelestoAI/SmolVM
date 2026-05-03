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

Resolution flow: ``ensure_published_image(preset, arch)`` looks up the
matching entry, converts it to an :class:`ImageSource`, and delegates to
:class:`ImageManager` for caching, downloading, and SHA-256 verification.
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


class PublishedImage(BaseModel):
    """One row of the published-image manifest."""

    preset: Preset
    arch: Arch
    kernel_url: str
    kernel_sha256: str
    rootfs_url: str
    rootfs_sha256: str

    model_config = {"frozen": True}


def release_tag(version: str = __version__) -> str:
    """GitHub Releases tag images for ``version`` are published under."""
    return f"images-v{version}"


def cache_name(preset: Preset, arch: Arch, version: str = __version__) -> str:
    """Cache directory name under ``~/.smolvm/images/``.

    Versioned + arch-suffixed so multiple installs and architectures
    coexist on the same machine without overwriting each other.
    """
    return f"{preset}-v{version}-{arch}"


# Bundled manifest. Empty until CI publishes its first ``images-v*``
# release. Append entries here keyed by ``(preset, arch)`` once the
# corresponding artifacts are uploaded to the matching release.
MANIFEST: dict[tuple[Preset, Arch], PublishedImage] = {}


def lookup(
    preset: Preset,
    arch: Arch,
    *,
    manifest: dict[tuple[Preset, Arch], PublishedImage] | None = None,
) -> PublishedImage:
    """Look up a manifest entry, raising :class:`ImageError` if missing."""
    catalog = MANIFEST if manifest is None else manifest
    entry = catalog.get((preset, arch))
    if entry is None:
        available = ", ".join(sorted(f"{p}/{a}" for (p, a) in catalog)) or "(none)"
        raise ImageError(
            f"No published image for preset '{preset}' on arch '{arch}' (available: {available})."
        )
    return entry


def to_image_source(entry: PublishedImage, version: str = __version__) -> ImageSource:
    """Convert a manifest entry into an :class:`ImageSource` for the manager."""
    return ImageSource(
        name=cache_name(entry.preset, entry.arch, version),
        kernel_url=entry.kernel_url,
        kernel_sha256=entry.kernel_sha256,
        rootfs_url=entry.rootfs_url,
        rootfs_sha256=entry.rootfs_sha256,
    )


def ensure_published_image(
    preset: Preset,
    arch: Arch,
    *,
    cache_dir: Path | None = None,
    manifest: dict[tuple[Preset, Arch], PublishedImage] | None = None,
    version: str = __version__,
) -> LocalImage:
    """Download (if needed) and return paths to a published preset image.

    Args:
        preset: Preset name (e.g. ``"codex"``).
        arch: Guest CPU architecture (``"amd64"`` or ``"arm64"``).
        cache_dir: Override the default cache directory (mainly for tests).
        manifest: Override the bundled manifest (mainly for tests).
        version: Override the CLI version used to compute the cache name.

    Returns:
        :class:`LocalImage` with paths to the kernel and rootfs.

    Raises:
        ImageError: If no manifest entry exists for the (preset, arch) pair,
            or if download / SHA-256 verification fails.
    """
    entry = lookup(preset, arch, manifest=manifest)
    source = to_image_source(entry, version=version)
    manager = ImageManager(cache_dir=cache_dir, registry={source.name: source})
    return manager.ensure_image(source.name)
