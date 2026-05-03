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

"""Tests for the published-image manifest and resolution path."""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.images.manager import LocalImage
from smolvm.images.published import (
    MANIFEST,
    Arch,
    Preset,
    PublishedImage,
    cache_name,
    ensure_published_image,
    lookup,
    release_tag,
    to_image_source,
)


@pytest.fixture
def sample_entry() -> PublishedImage:
    """A manifest entry with valid SHAs for the canned kernel/rootfs payload."""
    return PublishedImage(
        preset="codex",
        arch="amd64",
        kernel_url="https://example.com/codex-amd64-vmlinux.bin",
        kernel_sha256=hashlib.sha256(b"fake-kernel").hexdigest(),
        rootfs_url="https://example.com/codex-amd64-rootfs.ext4.zst",
        rootfs_sha256=hashlib.sha256(b"fake-rootfs").hexdigest(),
    )


@pytest.fixture
def sample_manifest(
    sample_entry: PublishedImage,
) -> dict[tuple[Preset, Arch], PublishedImage]:
    return {(sample_entry.preset, sample_entry.arch): sample_entry}


class TestNaming:
    def test_release_tag_uses_version(self) -> None:
        assert release_tag("0.0.13") == "images-v0.0.13"

    def test_cache_name_includes_preset_version_arch(self) -> None:
        assert cache_name("codex", "amd64", version="0.0.13") == "codex-v0.0.13-amd64"

    def test_cache_name_distinguishes_arches(self) -> None:
        amd = cache_name("codex", "amd64", version="0.0.13")
        arm = cache_name("codex", "arm64", version="0.0.13")
        assert amd != arm

    def test_cache_name_distinguishes_versions(self) -> None:
        v1 = cache_name("codex", "amd64", version="0.0.13")
        v2 = cache_name("codex", "amd64", version="0.0.14")
        assert v1 != v2


class TestLookup:
    def test_returns_matching_entry(
        self,
        sample_entry: PublishedImage,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
    ) -> None:
        assert lookup("codex", "amd64", manifest=sample_manifest) is sample_entry

    def test_missing_pair_raises_image_error(
        self,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
    ) -> None:
        with pytest.raises(ImageError, match="No published image for preset 'codex'"):
            lookup("codex", "arm64", manifest=sample_manifest)

    def test_error_lists_available_pairs(
        self,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
    ) -> None:
        with pytest.raises(ImageError, match="codex/amd64"):
            lookup("openclaw", "amd64", manifest=sample_manifest)

    def test_error_when_manifest_empty(self) -> None:
        with pytest.raises(ImageError, match=r"available: \(none\)"):
            lookup("codex", "amd64", manifest={})

    def test_default_manifest_used_when_not_overridden(self) -> None:
        # MANIFEST starts empty in this release; resolution should fail
        # with the helpful 'no published image' message rather than
        # silently 404'ing on a stale URL.
        if MANIFEST:
            pytest.skip("default manifest no longer empty")
        with pytest.raises(ImageError, match="No published image"):
            lookup("codex", "amd64")


class TestToImageSource:
    def test_propagates_urls_and_shas(self, sample_entry: PublishedImage) -> None:
        source = to_image_source(sample_entry, version="0.0.13")
        assert source.kernel_url == sample_entry.kernel_url
        assert source.kernel_sha256 == sample_entry.kernel_sha256
        assert source.rootfs_url == sample_entry.rootfs_url
        assert source.rootfs_sha256 == sample_entry.rootfs_sha256

    def test_name_uses_cache_name(self, sample_entry: PublishedImage) -> None:
        source = to_image_source(sample_entry, version="0.0.13")
        assert source.name == cache_name(sample_entry.preset, sample_entry.arch, version="0.0.13")


class TestEnsurePublishedImage:
    def test_returns_cached_without_download(
        self,
        tmp_path: Path,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
    ) -> None:
        version = "0.0.13"
        image_dir = tmp_path / cache_name("codex", "amd64", version=version)
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(b"fake-kernel")
        (image_dir / "rootfs.ext4").write_bytes(b"fake-rootfs")

        with patch("smolvm.images.manager.requests.get") as mock_get:
            local = ensure_published_image(
                "codex",
                "amd64",
                cache_dir=tmp_path,
                manifest=sample_manifest,
                version=version,
            )
            mock_get.assert_not_called()

        assert isinstance(local, LocalImage)
        assert local.kernel_path == image_dir / "vmlinux.bin"
        assert local.rootfs_path == image_dir / "rootfs.ext4"

    @patch("smolvm.images.manager.requests.get")
    def test_downloads_when_missing(
        self,
        mock_get: MagicMock,
        tmp_path: Path,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
    ) -> None:
        version = "0.0.13"
        bodies = [b"fake-kernel", b"fake-rootfs"]
        call = {"i": 0}

        def factory(*_args: object, **_kwargs: object) -> MagicMock:
            body = bodies[call["i"]]
            call["i"] += 1
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.headers.get.return_value = None
            resp.iter_content = lambda chunk_size, body=body: iter([body])
            return resp

        mock_get.side_effect = factory

        local = ensure_published_image(
            "codex",
            "amd64",
            cache_dir=tmp_path,
            manifest=sample_manifest,
            version=version,
        )

        assert local.kernel_path.read_bytes() == b"fake-kernel"
        assert local.rootfs_path.read_bytes() == b"fake-rootfs"
        assert mock_get.call_count == 2

    def test_unknown_pair_raises_before_touching_filesystem(
        self,
        tmp_path: Path,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
    ) -> None:
        with pytest.raises(ImageError, match="No published image"):
            ensure_published_image(
                "codex",
                "arm64",
                cache_dir=tmp_path,
                manifest=sample_manifest,
            )
        # No cache directory should have been created for the missing entry.
        assert list(tmp_path.iterdir()) == []
