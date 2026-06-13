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
import re
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.images import published as published_module
from smolvm.images.manager import LocalImage
from smolvm.images.published import (
    BASE_KERNELS,
    IMAGES_RELEASE_TAG,
    MANIFEST,
    Arch,
    BaseKernel,
    ManifestKey,
    Preset,
    PublishedImage,
    _decompress_zstd,
    _preset_rows,
    cache_name,
    ensure_base_kernel,
    ensure_published_image,
    is_preset_published,
    lookup,
    to_image_source,
)


def test_ubuntu_rows_share_one_rootfs_across_vmms() -> None:
    """The bare-Ubuntu image is one rootfs shared by firecracker/qemu/libkrun;
    only the kernel format differs. Guards the shared-rootfs invariant the
    `create --os ubuntu` firecracker path relies on."""
    rows = _preset_rows("ubuntu", "a" * 64, "b" * 64)
    fc = rows[("ubuntu", "amd64", "firecracker", "ubuntu")]
    qemu = rows[("ubuntu", "amd64", "qemu", "ubuntu")]

    # Same rootfs bytes + URL for both VMMs.
    assert fc.rootfs_url == qemu.rootfs_url
    assert fc.rootfs_sha256 == qemu.rootfs_sha256 == "a" * 64
    assert fc.rootfs_url.endswith("ubuntu-amd64-rootfs.ext4.zst")
    # Different kernel format: firecracker=elf, qemu=image.
    assert fc.kernel_url.endswith(".elf")
    assert qemu.kernel_url.endswith(".image")


def test_published_ubuntu_manifest_shares_rootfs_for_qemu_and_firecracker() -> None:
    """The live manifest keeps QEMU and Firecracker on the same Ubuntu rootfs."""
    qemu = MANIFEST[("ubuntu", "amd64", "qemu", "ubuntu")]
    firecracker = MANIFEST[("ubuntu", "amd64", "firecracker", "ubuntu")]

    assert qemu.rootfs_url == firecracker.rootfs_url
    assert qemu.rootfs_sha256 == firecracker.rootfs_sha256


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
        vmm="firecracker",
        os="ubuntu",
        kernel_url="https://example.com/codex-amd64-vmlinux.bin",
        kernel_sha256=hashlib.sha256(b"fake-kernel").hexdigest(),
        rootfs_url="https://example.com/codex-amd64-rootfs.ext4",
        rootfs_sha256=hashlib.sha256(b"fake-rootfs").hexdigest(),
    )


@pytest.fixture
def sample_manifest(
    sample_entry: PublishedImage,
) -> dict[ManifestKey, PublishedImage]:
    return {
        (sample_entry.preset, sample_entry.arch, sample_entry.vmm, sample_entry.os): sample_entry
    }


class TestNaming:
    def test_release_tag_constant_format(self) -> None:
        # Image/rootfs releases use CalVer because they are content snapshots,
        # not SmolVM package releases.
        assert re.fullmatch(r"images-\d{4}\.\d{2}\.\d{2}\.\d+", IMAGES_RELEASE_TAG)

    def test_release_url_uses_env_override_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMOLVM_IMAGES_RELEASE_TAG", "images-v0.0.16")

        assert published_module._images_release_tag() == "images-v0.0.16"
        assert "/images-v0.0.16/" in published_module._release_kernel_url("amd64", "elf")
        assert "/images-v0.0.16/" in published_module._release_asset_url(
            "openclaw", "amd64", "rootfs.ext4.zst"
        )

    def test_empty_release_tag_env_uses_pinned_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMOLVM_IMAGES_RELEASE_TAG", "")

        assert published_module._images_release_tag() == IMAGES_RELEASE_TAG

    def test_cache_name_includes_preset_version_arch_vmm(self) -> None:
        assert (
            cache_name("codex", "amd64", "firecracker", version="0.0.13")
            == "codex-v0.0.13-amd64-firecracker"
        )

    def test_cache_name_distinguishes_arches(self) -> None:
        amd = cache_name("codex", "amd64", "firecracker", version="0.0.13")
        arm = cache_name("codex", "arm64", "firecracker", version="0.0.13")
        assert amd != arm

    def test_cache_name_distinguishes_versions(self) -> None:
        v1 = cache_name("codex", "amd64", "firecracker", version="0.0.13")
        v2 = cache_name("codex", "amd64", "firecracker", version="0.0.14")
        assert v1 != v2

    def test_cache_name_distinguishes_vmms(self) -> None:
        """A user with both firecracker and qemu caches must not share a path."""
        fc = cache_name("openclaw", "amd64", "firecracker", version="0.0.13")
        qe = cache_name("openclaw", "amd64", "qemu", version="0.0.13")
        assert fc != qe

    def test_cache_name_ubuntu_omits_os_suffix(self) -> None:
        """Ubuntu cache names stay backward-compatible (no -ubuntu segment)
        so an existing on-disk cache from before the OS dimension landed
        is still picked up without forcing a re-download."""
        assert (
            cache_name("codex", "amd64", "firecracker", version="0.0.13", os="ubuntu")
            == "codex-v0.0.13-amd64-firecracker"
        )

    def test_cache_name_alpine_appends_os_suffix(self) -> None:
        """Alpine cache namespace is distinct from Ubuntu so users running
        both flavours don't share rootfs files."""
        assert (
            cache_name("codex", "amd64", "firecracker", version="0.0.13", os="alpine")
            == "codex-v0.0.13-amd64-firecracker-alpine"
        )

    def test_cache_name_distinguishes_os_flavors(self) -> None:
        ubu = cache_name("codex", "amd64", "firecracker", version="0.0.13", os="ubuntu")
        alp = cache_name("codex", "amd64", "firecracker", version="0.0.13", os="alpine")
        assert ubu != alp


class TestLookup:
    def test_returns_matching_entry(
        self,
        sample_entry: PublishedImage,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        assert lookup("codex", "amd64", "firecracker", manifest=sample_manifest) is sample_entry

    def test_missing_pair_raises_image_error(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        with pytest.raises(ImageError, match="No published image for preset 'codex'"):
            lookup("codex", "arm64", "firecracker", manifest=sample_manifest)

    def test_error_lists_available_tuples(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        with pytest.raises(ImageError, match="codex/amd64/firecracker"):
            lookup("openclaw", "amd64", "firecracker", manifest=sample_manifest)

    def test_error_when_manifest_empty(self) -> None:
        with pytest.raises(ImageError, match=r"available: \(none\)"):
            lookup("codex", "amd64", "firecracker", manifest={})

    def test_error_mentions_vmm_dimension(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        """Looking up a vmm that doesn't exist names it explicitly."""
        with pytest.raises(ImageError, match=r"vmm 'qemu'"):
            lookup("codex", "amd64", "qemu", manifest=sample_manifest)

    def test_default_manifest_used_when_not_overridden(self) -> None:
        # Verify lookup against the default (bundled) manifest finds known
        # entries and rejects unknown ones with the standard error.
        if not MANIFEST:
            pytest.skip("default manifest is empty in this release")
        first_key = next(iter(MANIFEST))
        assert lookup(*first_key) is MANIFEST[first_key]
        # An invented preset name is guaranteed to be missing — using a real
        # preset like "hermes" risks silently passing once it ships rows.
        with pytest.raises(ImageError, match="No published image"):
            lookup("definitely-not-a-real-preset", "arm64", "firecracker")  # type: ignore[arg-type]

    def test_default_os_is_ubuntu(
        self,
        sample_entry: PublishedImage,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        """Calling lookup without specifying os defaults to ubuntu."""
        # sample_entry has os="ubuntu", so the no-os call must hit it.
        assert lookup("codex", "amd64", "firecracker", manifest=sample_manifest) is sample_entry

    def test_alpine_lookup_misses_when_only_ubuntu_published(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        """Asking for alpine when only ubuntu is in the manifest must
        raise — falls through to install-at-boot at the CLI layer."""
        with pytest.raises(ImageError, match=r"os 'alpine'"):
            lookup("codex", "amd64", "firecracker", "alpine", manifest=sample_manifest)


class TestIsPresetPublished:
    def test_true_for_registered_entry(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        assert is_preset_published("codex", "amd64", "firecracker", manifest=sample_manifest)

    def test_false_for_missing_arch(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        assert not is_preset_published("codex", "arm64", "firecracker", manifest=sample_manifest)

    def test_false_for_missing_vmm(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        assert not is_preset_published("codex", "amd64", "qemu", manifest=sample_manifest)

    def test_false_for_unknown_preset(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        # Accepts arbitrary preset strings so the CLI doesn't need to coerce
        # against the Preset literal before dispatching.
        assert not is_preset_published(
            "claude-code", "amd64", "firecracker", manifest=sample_manifest
        )

    def test_accepts_arbitrary_preset_string(
        self,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        # Presets that aren't in the Preset literal must just return False, not
        # raise — the CLI passes user-typed preset names straight through.
        assert not is_preset_published(
            "totally-made-up", "amd64", "firecracker", manifest=sample_manifest
        )

    def test_empty_manifest_returns_false(self) -> None:
        assert not is_preset_published("codex", "amd64", "firecracker", manifest={})

    def test_default_manifest_used_when_not_overridden(self) -> None:
        # Cross-check against the bundled MANIFEST: at least one of its entries
        # must report True, and a guaranteed-missing tuple must report False.
        if not MANIFEST:
            pytest.skip("default manifest is empty in this release")
        preset, arch, vmm, os = next(iter(MANIFEST))
        assert is_preset_published(preset, arch, vmm, os)
        assert not is_preset_published("definitely-not-a-real-preset", arch, vmm, os)


class TestToImageSource:
    def test_propagates_urls_and_shas(self, sample_entry: PublishedImage) -> None:
        source = to_image_source(sample_entry, version="0.0.13")
        assert source.kernel_url == sample_entry.kernel_url
        assert source.kernel_sha256 == sample_entry.kernel_sha256
        assert source.rootfs_url == sample_entry.rootfs_url
        assert source.rootfs_sha256 == sample_entry.rootfs_sha256

    def test_name_uses_cache_name(self, sample_entry: PublishedImage) -> None:
        source = to_image_source(sample_entry, version="0.0.13")
        assert source.name == cache_name(
            sample_entry.preset,
            sample_entry.arch,
            sample_entry.vmm,
            version="0.0.13",
        )

    def test_name_uses_cache_name_for_alpine_entry(self) -> None:
        """Alpine entries must round-trip through cache_name with os='alpine'
        so the on-disk cache namespace matches what to_image_source produces."""
        entry = PublishedImage(
            preset="codex",
            arch="amd64",
            vmm="firecracker",
            os="alpine",
            kernel_url="https://example.com/codex-amd64-vmlinux.bin",
            kernel_sha256=hashlib.sha256(b"fake-kernel").hexdigest(),
            rootfs_url="https://example.com/codex-amd64-alpine-rootfs.ext4",
            rootfs_sha256=hashlib.sha256(b"fake-rootfs").hexdigest(),
        )
        source = to_image_source(entry, version="0.0.13")
        assert source.name == cache_name(
            entry.preset,
            entry.arch,
            entry.vmm,
            version="0.0.13",
            os="alpine",
        )
        # And the alpine and ubuntu cache names must differ for the same
        # (preset, arch, vmm) — otherwise running both flavours would
        # silently share rootfs files on disk.
        assert source.name != cache_name(
            entry.preset, entry.arch, entry.vmm, version="0.0.13", os="ubuntu"
        )


class TestEnsurePublishedImage:
    def test_returns_cached_without_download(
        self,
        tmp_path: Path,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        version = "0.0.13"
        image_dir = tmp_path / cache_name("codex", "amd64", "firecracker", version=version)
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(b"fake-kernel")
        (image_dir / "rootfs.ext4").write_bytes(b"fake-rootfs")

        with patch("smolvm.images.manager.requests.get") as mock_get:
            local = ensure_published_image(
                "codex",
                "amd64",
                "firecracker",
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
        sample_manifest: dict[ManifestKey, PublishedImage],
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
            "firecracker",
            cache_dir=tmp_path,
            manifest=sample_manifest,
            version=version,
        )

        assert local.kernel_path.read_bytes() == b"fake-kernel"
        assert local.rootfs_path.read_bytes() == b"fake-rootfs"
        assert mock_get.call_count == 2

    def test_unknown_tuple_raises_before_touching_filesystem(
        self,
        tmp_path: Path,
        sample_manifest: dict[ManifestKey, PublishedImage],
    ) -> None:
        with pytest.raises(ImageError, match="No published image"):
            ensure_published_image(
                "codex",
                "arm64",
                "firecracker",
                cache_dir=tmp_path,
                manifest=sample_manifest,
            )
        # No cache directory should have been created for the missing entry.
        assert list(tmp_path.iterdir()) == []

    def test_different_vmms_get_different_cache_dirs(
        self,
        tmp_path: Path,
    ) -> None:
        """Same (preset, arch) but different vmms must NOT share cache state.

        A user who runs both firecracker and qemu variants gets two cache
        directories. They share nothing — different kernels, possibly
        different rootfs URLs. Even if the rootfs is identical bytes, the
        cache layout keeps them isolated to avoid cross-vmm cache poisoning.
        """
        version = "0.0.13"
        # Pre-populate two distinct caches at the expected paths.
        for vmm in ("firecracker", "qemu"):
            image_dir = tmp_path / cache_name("codex", "amd64", vmm, version=version)  # type: ignore[arg-type]
            image_dir.mkdir(parents=True)
            (image_dir / "vmlinux.bin").write_bytes(f"kernel-{vmm}".encode())
            (image_dir / "rootfs.ext4").write_bytes(b"shared-rootfs")

        # Build a manifest with both vmm rows pointing at distinct kernel URLs.
        manifest: dict[ManifestKey, PublishedImage] = {}
        for vmm in ("firecracker", "qemu"):
            manifest[("codex", "amd64", vmm, "ubuntu")] = PublishedImage(  # type: ignore[index]
                preset="codex",
                arch="amd64",
                vmm=vmm,  # type: ignore[arg-type]
                os="ubuntu",
                kernel_url=f"https://example.com/codex-{vmm}-kernel",
                kernel_sha256=hashlib.sha256(f"kernel-{vmm}".encode()).hexdigest(),
                rootfs_url="https://example.com/codex-rootfs.ext4",
                rootfs_sha256=hashlib.sha256(b"shared-rootfs").hexdigest(),
            )

        with patch("smolvm.images.manager.requests.get") as mock_get:
            fc_local = ensure_published_image(
                "codex",
                "amd64",
                "firecracker",
                cache_dir=tmp_path,
                manifest=manifest,
                version=version,
            )
            qe_local = ensure_published_image(
                "codex",
                "amd64",
                "qemu",
                cache_dir=tmp_path,
                manifest=manifest,
                version=version,
            )
            mock_get.assert_not_called()

        # Distinct paths, distinct kernel bytes.
        assert fc_local.kernel_path != qe_local.kernel_path
        assert fc_local.kernel_path.read_bytes() == b"kernel-firecracker"
        assert qe_local.kernel_path.read_bytes() == b"kernel-qemu"


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
            vmm="firecracker",
            os="ubuntu",
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
        manifest = {(entry.preset, entry.arch, entry.vmm, entry.os): entry}
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
            entry.vmm,
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
        manifest = {(entry.preset, entry.arch, entry.vmm, entry.os): entry}

        # Pre-populate the cache: compressed file + decompressed sibling +
        # the SHA sidecar that keys the decompressed file to the .zst's
        # SHA. Without the sidecar matching the manifest's rootfs_sha256,
        # ensure_published_image re-decompresses (intended behavior — see
        # the cache-invalidation comment in published.py).
        image_dir = tmp_path / cache_name(entry.preset, entry.arch, entry.vmm, "0.0.13")
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(kernel_bytes)
        (image_dir / "rootfs.ext4.zst").write_bytes(rootfs_zst)
        (image_dir / "rootfs.ext4").write_bytes(rootfs_plain)
        (image_dir / "rootfs.ext4.from-sha256").write_text(entry.rootfs_sha256)

        with patch("smolvm.images.published._decompress_zstd") as mock_decompress:
            local = ensure_published_image(
                entry.preset,
                entry.arch,
                entry.vmm,
                cache_dir=tmp_path,
                manifest=manifest,
                version="0.0.13",
            )
            mock_decompress.assert_not_called()
            mock_get.assert_not_called()

        assert local.rootfs_path.read_bytes() == rootfs_plain

    @patch("smolvm.images.manager.requests.get")
    def test_decompression_reruns_when_zst_sha_changes(
        self,
        mock_get: MagicMock,
        compressed_entry: tuple[PublishedImage, bytes, bytes, bytes],
        tmp_path: Path,
    ) -> None:
        """A refreshed .zst (different SHA) must re-decompress, not silently
        serve the stale .ext4 from the previous SHA. The bug this guards
        against: the openclaw rootfs uid bake regression hid behind a
        cached decompressed .ext4 even after the .zst was re-fetched."""
        entry, kernel_bytes, rootfs_zst, rootfs_plain = compressed_entry
        manifest = {(entry.preset, entry.arch, entry.vmm, entry.os): entry}

        image_dir = tmp_path / cache_name(entry.preset, entry.arch, entry.vmm, "0.0.13")
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(kernel_bytes)
        (image_dir / "rootfs.ext4.zst").write_bytes(rootfs_zst)
        (image_dir / "rootfs.ext4").write_bytes(rootfs_plain)
        # Sidecar from a PREVIOUS rootfs SHA (not the one in `entry`).
        (image_dir / "rootfs.ext4.from-sha256").write_text("0" * 64)

        with patch("smolvm.images.published._decompress_zstd") as mock_decompress:
            ensure_published_image(
                entry.preset,
                entry.arch,
                entry.vmm,
                cache_dir=tmp_path,
                manifest=manifest,
                version="0.0.13",
            )
            mock_decompress.assert_called_once()
            mock_get.assert_not_called()

        assert (image_dir / "rootfs.ext4.from-sha256").read_text().strip() == entry.rootfs_sha256

    @patch("smolvm.images.manager.requests.get")
    def test_uncompressed_rootfs_url_skips_decompression_path(
        self,
        mock_get: MagicMock,
        sample_entry: PublishedImage,
        sample_manifest: dict[ManifestKey, PublishedImage],
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
                sample_entry.vmm,
                cache_dir=tmp_path,
                manifest=sample_manifest,
                version="0.0.13",
            )
            mock_decompress.assert_not_called()

        assert local.rootfs_path.name == "rootfs.ext4"
        assert local.rootfs_path.read_bytes() == b"fake-rootfs"


class TestBaseKernels:
    """Sanity checks for the SmolVM-built kernels (BASE_KERNELS).

    Each entry carries TWO formats — ELF for Firecracker, Image for QEMU —
    from a single source build. See :class:`BaseKernel` docstring.
    """

    def test_amd64_entry_shape(self) -> None:
        entry = BASE_KERNELS.get("amd64")
        assert entry is not None
        assert isinstance(entry, BaseKernel)
        assert entry.arch == "amd64"
        assert len(entry.elf_sha256) == 64
        assert len(entry.image_sha256) == 64
        assert entry.elf_url.endswith("vmlinux-amd64.elf")
        assert entry.image_url.endswith("vmlinux-amd64.image")
        assert "images-" in entry.elf_url

    def test_arm64_entry_shape(self) -> None:
        entry = BASE_KERNELS.get("arm64")
        assert entry is not None
        assert entry.arch == "arm64"
        assert entry.elf_url.endswith("vmlinux-arm64.elf")
        assert entry.image_url.endswith("vmlinux-arm64.image")

    def test_url_for_format_dispatches(self) -> None:
        amd = BASE_KERNELS["amd64"]
        assert amd.url_for("elf") == amd.elf_url
        assert amd.url_for("image") == amd.image_url
        assert amd.sha256_for("elf") == amd.elf_sha256
        assert amd.sha256_for("image") == amd.image_sha256

    def test_amd64_and_arm64_have_distinct_urls(self) -> None:
        """Sanity: per-arch URLs are distinct (catches copy-paste regressions)."""
        amd = BASE_KERNELS["amd64"]
        arm = BASE_KERNELS["arm64"]
        assert amd.elf_url != arm.elf_url
        assert amd.image_url != arm.image_url

    def test_elf_and_image_urls_are_distinct(self) -> None:
        """Format URLs must point at different artifacts."""
        for arch in ("amd64", "arm64"):
            entry = BASE_KERNELS[arch]
            assert entry.elf_url != entry.image_url

    def test_manifest_rows_reuse_base_kernel_shas(self) -> None:
        """Every MANIFEST row's kernel must mirror the matching BASE_KERNELS
        entry's URL+SHA for the format implied by its vmm.

        BASE_KERNELS is the source of truth — if a manifest row drifts
        (e.g. someone hand-edits a SHA), SHA verification at download
        time would fail.
        """
        from smolvm.images.published import _kernel_format_for_vmm

        for key, row in MANIFEST.items():
            _preset, arch, vmm, _os = key
            base = BASE_KERNELS[arch]
            fmt = _kernel_format_for_vmm(vmm)
            assert row.kernel_url == base.url_for(fmt), f"{key} kernel_url drift"
            assert row.kernel_sha256 == base.sha256_for(fmt), f"{key} kernel_sha256 drift"

    def test_ensure_base_kernel_unknown_arch_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ImageError, match="No base kernel registered"):
            ensure_base_kernel("riscv64", "elf", cache_dir=tmp_path, registry={})  # type: ignore[arg-type]

    def test_ensure_base_kernel_uses_cache_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ensure_base_kernel should land at base-kernel-v<version>-<arch>/vmlinux.<fmt>
        and request the right SHA per format."""
        import smolvm.images.published as published

        captured: dict[str, object] = {}

        def fake_ensure_rootfs_only(
            self_: object,  # noqa: ARG001 (bound method receiver)
            name: str,
            *,
            url: str,
            filename: str,
            sha256: str | None = None,
            on_download: object = None,  # noqa: ARG001 (matches real signature)
        ) -> Path:
            captured["name"] = name
            captured["url"] = url
            captured["filename"] = filename
            captured["sha256"] = sha256
            return tmp_path / filename

        monkeypatch.setattr(
            published.ImageManager,
            "ensure_rootfs_only",
            fake_ensure_rootfs_only,
        )

        path_elf = ensure_base_kernel("amd64", "elf", cache_dir=tmp_path, version="9.9.9z")
        assert path_elf == tmp_path / "vmlinux.elf"
        assert captured["name"] == "base-kernel-v9.9.9z-amd64"
        assert captured["filename"] == "vmlinux.elf"
        assert captured["sha256"] == BASE_KERNELS["amd64"].elf_sha256
        assert captured["url"] == BASE_KERNELS["amd64"].elf_url

        path_img = ensure_base_kernel("amd64", "image", cache_dir=tmp_path, version="9.9.9z")
        assert path_img == tmp_path / "vmlinux.image"
        assert captured["filename"] == "vmlinux.image"
        assert captured["sha256"] == BASE_KERNELS["amd64"].image_sha256
        assert captured["url"] == BASE_KERNELS["amd64"].image_url


class TestBundledManifest:
    """Sanity checks for the entries hand-populated in MANIFEST."""

    def test_openclaw_amd64_firecracker_entry_shape(self) -> None:
        entry = MANIFEST.get(("openclaw", "amd64", "firecracker", "ubuntu"))
        assert entry is not None, "openclaw/amd64/firecracker/ubuntu must be in MANIFEST"
        assert len(entry.rootfs_sha256) == 64  # SHA-256 hex
        assert len(entry.kernel_sha256) == 64
        assert entry.rootfs_url.endswith("openclaw-amd64-rootfs.ext4.zst")
        # Firecracker rows get the ELF-format kernel.
        assert entry.kernel_url.endswith("vmlinux-amd64.elf")
        assert "images-" in entry.rootfs_url

    def test_openclaw_arm64_firecracker_entry_shape(self) -> None:
        entry = MANIFEST.get(("openclaw", "arm64", "firecracker", "ubuntu"))
        assert entry is not None
        assert len(entry.rootfs_sha256) == 64
        assert entry.rootfs_url.endswith("openclaw-arm64-rootfs.ext4.zst")
        assert entry.kernel_url.endswith("vmlinux-arm64.elf")

    def test_openclaw_qemu_rows_use_image_format(self) -> None:
        """QEMU rows must use the Image-format kernel — see _kernel_format_for_vmm."""
        amd_qemu = MANIFEST[("openclaw", "amd64", "qemu", "ubuntu")]
        arm_qemu = MANIFEST[("openclaw", "arm64", "qemu", "ubuntu")]
        assert amd_qemu.kernel_url.endswith("vmlinux-amd64.image")
        assert arm_qemu.kernel_url.endswith("vmlinux-arm64.image")

    def test_arches_have_distinct_rootfs_shas(self) -> None:
        """Sanity: copy-paste error would give both arches the same rootfs SHA."""
        amd = MANIFEST[("openclaw", "amd64", "firecracker", "ubuntu")]
        arm = MANIFEST[("openclaw", "arm64", "firecracker", "ubuntu")]
        assert amd.rootfs_sha256 != arm.rootfs_sha256
        assert amd.rootfs_url != arm.rootfs_url

    def test_all_entries_use_release_tag_in_url(self) -> None:
        """Every entry's URL must reference the pinned release tag."""
        expected_segment = f"/{IMAGES_RELEASE_TAG}/"
        for key, entry in MANIFEST.items():
            assert expected_segment in entry.rootfs_url, (
                f"{key} rootfs_url doesn't reference {expected_segment}"
            )
            assert expected_segment in entry.kernel_url, (
                f"{key} kernel_url doesn't reference {expected_segment}"
            )

    def test_entry_field_matches_manifest_key(self) -> None:
        """The fields on each row must match the (preset, arch, vmm, os) tuple it lives under."""
        for key, entry in MANIFEST.items():
            preset, arch, vmm, os = key
            assert entry.preset == preset, f"row at {key} has preset={entry.preset!r}"
            assert entry.arch == arch, f"row at {key} has arch={entry.arch!r}"
            assert entry.vmm == vmm, f"row at {key} has vmm={entry.vmm!r}"
            assert entry.os == os, f"row at {key} has os={entry.os!r}"

    def test_rootfs_sha_is_consistent_across_vmm_variants(self) -> None:
        """For any (preset, arch, os), all vmm rows must share the same rootfs SHA.

        Rationale: the rootfs is filesystem-format only — VMM-agnostic. Different
        vmm rows for the same (preset, arch, os) point at the SAME rootfs file. If
        someone updates only the firecracker row's SHA after a rootfs rebuild
        and forgets the qemu row, downloads silently 404 (or worse, mismatch).
        This test catches that.

        The check is conservative: if a future need actually requires
        per-vmm rootfs differences (unlikely — the rootfs doesn't see the
        VMM), this assertion needs revisiting along with the "shared rootfs"
        design assumption.
        """
        from smolvm.images.published import Os

        by_preset_arch_os: dict[tuple[Preset, Arch, Os], list[PublishedImage]] = defaultdict(list)
        for key, entry in MANIFEST.items():
            preset, arch, _vmm, os = key
            by_preset_arch_os[(preset, arch, os)].append(entry)

        for (preset, arch, os), entries in by_preset_arch_os.items():
            if len(entries) < 2:
                continue  # nothing to compare
            shas = {e.rootfs_sha256 for e in entries}
            urls = {e.rootfs_url for e in entries}
            assert len(shas) == 1, (
                f"{preset}/{arch}/{os}: rootfs SHAs differ across vmm rows: {sorted(shas)}. "
                f"Likely one row was updated after a rootfs rebuild and another wasn't."
            )
            assert len(urls) == 1, (
                f"{preset}/{arch}/{os}: rootfs URLs differ across vmm rows: {sorted(urls)}."
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
