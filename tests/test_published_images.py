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
    _MANIFEST_VERSION,
    MANIFEST,
    Arch,
    Preset,
    PublishedImage,
    _decompress_zstd,
    cache_name,
    ensure_published_image,
    lookup,
    release_tag,
    to_image_source,
)


@pytest.fixture
def sample_entry() -> PublishedImage:
    """A manifest entry pointing at an UNCOMPRESSED rootfs.

    Most resolution tests don't care about the compression path; using an
    uncompressed URL keeps the fixture data simple. See ``compressed_entry``
    for the .zst-handling tests.
    """
    return PublishedImage(
        preset="codex",
        arch="amd64",
        kernel_url="https://example.com/codex-amd64-vmlinux.bin",
        kernel_sha256=hashlib.sha256(b"fake-kernel").hexdigest(),
        rootfs_url="https://example.com/codex-amd64-rootfs.ext4",
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
        # The bundled MANIFEST is now populated with at least one entry
        # (openclaw amd64). Verify lookup against the default manifest
        # finds known entries and rejects unknown ones with the standard
        # error message — same behavior either way.
        if not MANIFEST:
            pytest.skip("default manifest is empty in this release")
        # An entry that exists in the default manifest:
        first_key = next(iter(MANIFEST))
        assert lookup(*first_key) is MANIFEST[first_key]
        # And one that's guaranteed not to:
        with pytest.raises(ImageError, match="No published image"):
            lookup("hermes", "arm64")  # type: ignore[arg-type]


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


class TestZstdDecompression:
    """Tests for the .zst rootfs decompression path.

    Published images ship as zstd-compressed rootfs files (~5-7x smaller
    on the wire). ImageManager downloads + SHA-verifies the compressed
    bytes; ensure_published_image then decompresses alongside.
    """

    @pytest.fixture
    def compressed_entry(self) -> tuple[PublishedImage, bytes, bytes, bytes]:
        """A manifest entry whose rootfs SHA matches a real zstd payload.

        Returns ``(entry, kernel_bytes, rootfs_zst, rootfs_plain)`` so each
        test can both feed the right wire bytes to the mock HTTP handler
        AND assert on the decompressed payload.
        """
        import zstandard

        kernel_bytes = b"fake-kernel-bytes"
        rootfs_plain = b"this is the plaintext rootfs content for testing"
        rootfs_zst = zstandard.ZstdCompressor(level=3).compress(rootfs_plain)

        entry = PublishedImage(
            preset="codex",
            arch="amd64",
            kernel_url="https://example.com/codex-amd64-vmlinux.bin",
            kernel_sha256=hashlib.sha256(kernel_bytes).hexdigest(),
            rootfs_url="https://example.com/codex-amd64-rootfs.ext4.zst",
            rootfs_sha256=hashlib.sha256(rootfs_zst).hexdigest(),
        )
        return entry, kernel_bytes, rootfs_zst, rootfs_plain

    @patch("smolvm.images.manager.requests.get")
    def test_compressed_rootfs_is_decompressed_after_download(
        self,
        mock_get: MagicMock,
        compressed_entry: tuple[PublishedImage, bytes, bytes, bytes],
        tmp_path: Path,
    ) -> None:
        entry, kernel_bytes, rootfs_zst, rootfs_plain = compressed_entry
        manifest = {(entry.preset, entry.arch): entry}
        bodies = [kernel_bytes, rootfs_zst]
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
            entry.preset,
            entry.arch,
            cache_dir=tmp_path,
            manifest=manifest,
            version="0.0.13",
        )

        assert local.rootfs_path.name == "rootfs.ext4"  # decompressed sibling
        assert local.rootfs_path.read_bytes() == rootfs_plain
        # The compressed file is kept alongside so SHA verification can
        # re-run on subsequent calls without re-downloading.
        assert (local.rootfs_path.parent / "rootfs.ext4.zst").is_file()

    @patch("smolvm.images.manager.requests.get")
    def test_decompression_skipped_on_subsequent_call(
        self,
        mock_get: MagicMock,
        compressed_entry: tuple[PublishedImage, bytes, bytes, bytes],
        tmp_path: Path,
    ) -> None:
        """Second call must not re-download AND not re-decompress."""
        entry, kernel_bytes, rootfs_zst, rootfs_plain = compressed_entry
        manifest = {(entry.preset, entry.arch): entry}

        # Pre-populate the cache: compressed file + decompressed sibling.
        image_dir = tmp_path / cache_name(entry.preset, entry.arch, "0.0.13")
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(kernel_bytes)
        (image_dir / "rootfs.ext4.zst").write_bytes(rootfs_zst)
        (image_dir / "rootfs.ext4").write_bytes(rootfs_plain)

        with patch("smolvm.images.published._decompress_zstd") as mock_decompress:
            local = ensure_published_image(
                entry.preset,
                entry.arch,
                cache_dir=tmp_path,
                manifest=manifest,
                version="0.0.13",
            )
            mock_decompress.assert_not_called()
            mock_get.assert_not_called()

        assert local.rootfs_path.read_bytes() == rootfs_plain

    @patch("smolvm.images.manager.requests.get")
    def test_uncompressed_rootfs_url_skips_decompression_path(
        self,
        mock_get: MagicMock,
        sample_entry: PublishedImage,
        sample_manifest: dict[tuple[Preset, Arch], PublishedImage],
        tmp_path: Path,
    ) -> None:
        """A non-.zst rootfs URL must not invoke the decompressor."""
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

        with patch("smolvm.images.published._decompress_zstd") as mock_decompress:
            local = ensure_published_image(
                sample_entry.preset,
                sample_entry.arch,
                cache_dir=tmp_path,
                manifest=sample_manifest,
                version="0.0.13",
            )
            mock_decompress.assert_not_called()

        assert local.rootfs_path.name == "rootfs.ext4"
        assert local.rootfs_path.read_bytes() == b"fake-rootfs"


class TestBundledManifest:
    """Sanity checks for the entries hand-populated in MANIFEST."""

    def test_openclaw_amd64_entry_shape(self) -> None:
        entry = MANIFEST.get(("openclaw", "amd64"))
        assert entry is not None, "openclaw/amd64 must be in the bundled manifest"
        assert len(entry.rootfs_sha256) == 64  # SHA-256 hex
        assert len(entry.kernel_sha256) == 64
        assert entry.rootfs_url.endswith("openclaw-amd64-rootfs.ext4.zst")
        assert entry.kernel_url.endswith("openclaw-amd64-vmlinux.bin")
        assert "images-v" in entry.rootfs_url  # tag-based URL pattern

    def test_openclaw_arm64_entry_shape(self) -> None:
        entry = MANIFEST.get(("openclaw", "arm64"))
        assert entry is not None
        assert len(entry.rootfs_sha256) == 64
        assert entry.rootfs_url.endswith("openclaw-arm64-rootfs.ext4.zst")

    def test_amd64_and_arm64_have_distinct_shas(self) -> None:
        """Sanity: copy-paste error would give both arches the same SHA."""
        amd = MANIFEST[("openclaw", "amd64")]
        arm = MANIFEST[("openclaw", "arm64")]
        assert amd.rootfs_sha256 != arm.rootfs_sha256
        assert amd.kernel_sha256 != arm.kernel_sha256

    def test_manifest_version_matches_cli_version(self) -> None:
        """Catch drift if pyproject.toml is bumped without regenerating MANIFEST.

        The bundled MANIFEST entries point at images-v<_MANIFEST_VERSION>.
        Shipping a CLI release whose __version__ doesn't match would have
        the CLI claim to be vX.Y.Z while pulling images for vA.B.C — a
        subtle inconsistency that surfaces as 404s from URLs the manifest
        no longer accurately describes.
        """
        from smolvm import __version__

        assert __version__ == _MANIFEST_VERSION, (
            f"MANIFEST is for v{_MANIFEST_VERSION} but CLI is v{__version__}. "
            f"Either bump _MANIFEST_VERSION + regenerate the entries from a "
            f"fresh CI run, or revert the pyproject.toml version bump."
        )

    def test_all_entries_use_manifest_version_in_url(self) -> None:
        """Every entry's URL must reference the same release tag we claim."""
        expected_segment = f"/images-v{_MANIFEST_VERSION}/"
        for key, entry in MANIFEST.items():
            assert expected_segment in entry.rootfs_url, (
                f"{key} rootfs_url doesn't reference {expected_segment}"
            )
            assert expected_segment in entry.kernel_url, (
                f"{key} kernel_url doesn't reference {expected_segment}"
            )


class TestDecompressZstd:
    """Direct tests for the streaming decompressor."""

    def test_corrupted_input_cleans_up_tmp_file(self, tmp_path: Path) -> None:
        """A failed decompress must not leave a half-written ``.tmp`` behind."""
        import zstandard

        src = tmp_path / "corrupt.ext4.zst"
        src.write_bytes(b"this is definitely not a valid zstd stream")
        dst = tmp_path / "corrupt.ext4"

        with pytest.raises(zstandard.ZstdError):
            _decompress_zstd(src, dst)

        # Neither the destination nor the .tmp sibling should remain.
        assert not dst.exists()
        assert not (dst.parent / (dst.name + ".tmp")).exists()
