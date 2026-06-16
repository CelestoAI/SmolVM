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

"""Tests for SmolVM images module."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.images.manager import (
    ImageManager,
    ImageSource,
    LocalImage,
    S3ImageManifest,
    S3ImageRef,
    parse_s3_image_uri,
)


@pytest.fixture
def image_registry() -> dict[str, ImageSource]:
    """Create a test image registry with known checksums."""
    kernel_content = b"fake-kernel-binary-content"
    rootfs_content = b"fake-rootfs-binary-content"

    return {
        "test-image": ImageSource(
            name="test-image",
            kernel_url="https://example.com/vmlinux.bin",
            kernel_sha256=hashlib.sha256(kernel_content).hexdigest(),
            rootfs_url="https://example.com/rootfs.ext4",
            rootfs_sha256=hashlib.sha256(rootfs_content).hexdigest(),
        ),
    }


@pytest.fixture
def image_manager(tmp_path: Path, image_registry: dict[str, ImageSource]) -> ImageManager:
    """Create an ImageManager with temp cache and test registry."""
    return ImageManager(cache_dir=tmp_path / "images", registry=image_registry)


class TestListAvailable:
    """Tests for listing available images."""

    def test_list_builtin_images(self, image_manager: ImageManager) -> None:
        """Test listing registered image names."""
        names = image_manager.list_available()
        assert "test-image" in names

    def test_list_returns_sorted(self, tmp_path: Path) -> None:
        """Test that names are sorted."""
        registry = {
            "zeta": ImageSource(
                name="zeta",
                kernel_url="x",
                kernel_sha256="x",
                rootfs_url="x",
                rootfs_sha256="x",
            ),
            "alpha": ImageSource(
                name="alpha",
                kernel_url="x",
                kernel_sha256="x",
                rootfs_url="x",
                rootfs_sha256="x",
            ),
        }
        mgr = ImageManager(cache_dir=tmp_path, registry=registry)
        assert mgr.list_available() == ["alpha", "zeta"]


class TestImageSourceValidation:
    """Tests for ImageSource filename normalization and validation."""

    def test_normalizes_cache_filenames_to_basenames(self) -> None:
        """Cache filenames should be sanitized to their basename."""
        source = ImageSource(
            name="test-image",
            kernel_url="https://example.com/vmlinux.bin",
            kernel_filename="nested/kernel/vmlinux.bin",
            initrd_url="https://example.com/initrd.img",
            initrd_filename="nested/initrd/initrd.img",
            rootfs_url="https://example.com/rootfs.ext4",
            rootfs_filename="nested/rootfs/rootfs.ext4",
        )

        assert source.kernel_filename == "vmlinux.bin"
        assert source.initrd_filename == "initrd.img"
        assert source.rootfs_filename == "rootfs.ext4"

    def test_rejects_absolute_cache_filenames(self) -> None:
        """Absolute cache filenames should be rejected."""
        with pytest.raises(ValueError, match="relative paths"):
            ImageSource(
                name="test-image",
                kernel_url="https://example.com/vmlinux.bin",
                kernel_filename="/tmp/vmlinux.bin",
                rootfs_url="https://example.com/rootfs.ext4",
            )


class TestIsCached:
    """Tests for cache checking."""

    def test_not_cached(self, image_manager: ImageManager) -> None:
        """Test that uncached image returns False."""
        assert image_manager.is_cached("test-image") is False

    def test_cached(self, image_manager: ImageManager) -> None:
        """Test that cached image returns True."""
        image_dir = image_manager.cache_dir / "test-image"
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(b"kernel")
        (image_dir / "rootfs.ext4").write_bytes(b"rootfs")

        assert image_manager.is_cached("test-image") is True

    def test_partially_cached(self, image_manager: ImageManager) -> None:
        """Test that partially cached image returns False."""
        image_dir = image_manager.cache_dir / "test-image"
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(b"kernel")
        # rootfs is missing

        assert image_manager.is_cached("test-image") is False

    def test_empty_name_raises(self, image_manager: ImageManager) -> None:
        """Test that empty image name raises ValueError."""
        with pytest.raises(ValueError, match="image name cannot be empty"):
            image_manager.is_cached("")


class TestEnsureImage:
    """Tests for ensuring images are available."""

    def test_unknown_image_raises(self, image_manager: ImageManager) -> None:
        """Test that requesting unknown image raises ImageError."""
        with pytest.raises(ImageError, match="Unknown image"):
            image_manager.ensure_image("nonexistent")

    def test_empty_name_raises(self, image_manager: ImageManager) -> None:
        """Test that empty image name raises ValueError."""
        with pytest.raises(ValueError, match="image name cannot be empty"):
            image_manager.ensure_image("")

    def test_rejects_colliding_asset_filenames(self, tmp_path: Path) -> None:
        """Image assets should not be allowed to collide inside one cache directory."""
        registry = {
            "colliding": ImageSource(
                name="colliding",
                kernel_url="https://example.com/vmlinux.bin",
                kernel_filename="shared.bin",
                rootfs_url="https://example.com/rootfs.ext4",
                rootfs_filename="nested/shared.bin",
            ),
        }
        mgr = ImageManager(cache_dir=tmp_path / "images", registry=registry)

        with pytest.raises(ImageError, match="filenames collide"):
            mgr.ensure_image("colliding")

    def test_returns_cached_without_download(
        self, image_manager: ImageManager, image_registry: dict[str, ImageSource]
    ) -> None:
        """Test that cached image is returned without downloading."""
        # Pre-populate cache with correct checksums
        image_dir = image_manager.cache_dir / "test-image"
        image_dir.mkdir(parents=True)

        kernel_content = b"fake-kernel-binary-content"
        rootfs_content = b"fake-rootfs-binary-content"
        (image_dir / "vmlinux.bin").write_bytes(kernel_content)
        (image_dir / "rootfs.ext4").write_bytes(rootfs_content)

        with patch("smolvm.images.manager.requests.get") as mock_get:
            result = image_manager.ensure_image("test-image")

            # Should NOT have downloaded anything
            mock_get.assert_not_called()

        assert isinstance(result, LocalImage)
        assert result.name == "test-image"
        assert result.kernel_path.exists()
        assert result.rootfs_path.exists()

    @patch("smolvm.images.manager.requests.get")
    def test_downloads_when_not_cached(
        self,
        mock_get: MagicMock,
        image_manager: ImageManager,
        image_registry: dict[str, ImageSource],
    ) -> None:
        """Test that missing image is downloaded."""
        kernel_content = b"fake-kernel-binary-content"
        rootfs_content = b"fake-rootfs-binary-content"

        call_count = 0

        def mock_response_factory(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            # First call is kernel, second is rootfs
            content = kernel_content if call_count == 1 else rootfs_content
            mock_resp.iter_content = lambda chunk_size: iter([content])
            return mock_resp

        mock_get.side_effect = mock_response_factory

        result = image_manager.ensure_image("test-image")

        assert isinstance(result, LocalImage)
        assert result.kernel_path.exists()
        assert result.rootfs_path.exists()
        assert mock_get.call_count == 2

    @patch("smolvm.images.manager.requests.get")
    def test_re_downloads_on_sha_mismatch(
        self,
        mock_get: MagicMock,
        image_manager: ImageManager,
        image_registry: dict[str, ImageSource],
    ) -> None:
        """Test that corrupted cache triggers re-download."""
        # Pre-populate with bad content
        image_dir = image_manager.cache_dir / "test-image"
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(b"corrupted")
        (image_dir / "rootfs.ext4").write_bytes(b"corrupted")

        kernel_content = b"fake-kernel-binary-content"
        rootfs_content = b"fake-rootfs-binary-content"

        call_count = 0

        def mock_response_factory(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            content = kernel_content if call_count == 1 else rootfs_content
            mock_resp.iter_content = lambda chunk_size: iter([content])
            return mock_resp

        mock_get.side_effect = mock_response_factory

        result = image_manager.ensure_image("test-image")

        assert result.kernel_path.exists()
        assert mock_get.call_count == 2  # Both re-downloaded


class TestDownloadFile:
    """Tests for atomic download."""

    @patch("smolvm.images.manager.requests.get")
    def test_sha_mismatch_raises(
        self, mock_get: MagicMock, image_manager: ImageManager, tmp_path: Path
    ) -> None:
        """Test that SHA-256 mismatch raises ImageError."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content = lambda chunk_size: iter([b"wrong content"])
        mock_get.return_value = mock_resp

        dest = tmp_path / "output.bin"

        with pytest.raises(ImageError, match="SHA-256 mismatch"):
            image_manager._download_file(
                "https://example.com/file",
                dest,
                "0000000000000000000000000000000000000000000000000000000000000000",
            )

        # Temp file should be cleaned up
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp"))

    @patch("smolvm.images.manager.requests.get")
    def test_network_error_raises(
        self, mock_get: MagicMock, image_manager: ImageManager, tmp_path: Path
    ) -> None:
        """Test that network error raises ImageError."""
        import requests

        mock_get.side_effect = requests.ConnectionError("no network")

        dest = tmp_path / "output.bin"

        with pytest.raises(ImageError, match="Download failed"):
            image_manager._download_file("https://example.com/file", dest, "abc123")


class TestVerifySHA256:
    """Tests for SHA-256 verification."""

    def test_correct_hash(self, tmp_path: Path) -> None:
        """Test verification with correct hash."""
        content = b"hello world"
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(content)

        expected = hashlib.sha256(content).hexdigest()
        assert ImageManager._verify_sha256(file_path, expected) is True

    def test_wrong_hash(self, tmp_path: Path) -> None:
        """Test verification with wrong hash."""
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(b"hello world")

        assert ImageManager._verify_sha256(file_path, "0" * 64) is False

    def test_none_hash_skips_verification(self, tmp_path: Path) -> None:
        """Test that None expected hash returns True (skip verification)."""
        file_path = tmp_path / "test.bin"
        file_path.write_bytes(b"any content")

        assert ImageManager._verify_sha256(file_path, None) is True

    @patch("smolvm.images.manager.requests.get")
    def test_download_skips_sha_when_none(self, mock_get: MagicMock, tmp_path: Path) -> None:
        """Test that _download_file succeeds without SHA check when None."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content = lambda chunk_size: iter([b"file content"])
        mock_get.return_value = mock_resp

        mgr = ImageManager(cache_dir=tmp_path)
        dest = tmp_path / "output.bin"

        # Should NOT raise even with no SHA
        mgr._download_file("https://example.com/file", dest, None)
        assert dest.exists()

    def test_ensure_image_cache_hit_without_sha(self, tmp_path: Path) -> None:
        """Test that cached image is returned when SHA is None (no re-download)."""
        registry = {
            "no-sha": ImageSource(
                name="no-sha",
                kernel_url="https://example.com/vmlinux.bin",
                rootfs_url="https://example.com/rootfs.ext4",
                # sha256 fields default to None
            ),
        }
        mgr = ImageManager(cache_dir=tmp_path / "images", registry=registry)

        # Pre-populate cache
        image_dir = mgr.cache_dir / "no-sha"
        image_dir.mkdir(parents=True)
        (image_dir / "vmlinux.bin").write_bytes(b"kernel")
        (image_dir / "rootfs.ext4").write_bytes(b"rootfs")

        with patch("smolvm.images.manager.requests.get") as mock_get:
            result = mgr.ensure_image("no-sha")
            mock_get.assert_not_called()

        assert result.name == "no-sha"
        assert result.kernel_path.exists()


class TestEnsureRootfsOnly:
    """Tests for ImageManager.ensure_rootfs_only."""

    def test_strips_path_traversal_from_filename(self, tmp_path: Path) -> None:
        """A filename with '..' components must be basenamed, never escape cache."""
        mgr = ImageManager(cache_dir=tmp_path)

        captured_dest: list[Path] = []

        def fake_download(
            self: ImageManager,
            url: str,
            dest: Path,
            expected_sha256: str | None = None,
            *,
            progress_callback: object = None,
        ) -> None:
            captured_dest.append(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"ok")

        with patch.object(ImageManager, "_download_file", fake_download):
            result = mgr.ensure_rootfs_only(
                "evil-image",
                url="https://example.com/rootfs.qcow2",
                filename="../../../etc/passwd",
            )

        # The destination must stay inside cache_dir/evil-image/, with only
        # the basename ("passwd") retained from the caller input.
        assert len(captured_dest) == 1
        dest = captured_dest[0]
        assert dest == tmp_path / "evil-image" / "passwd"
        assert result == dest
        # Sanity check: the attacker-controlled traversal target does not exist.
        assert not (tmp_path / "etc" / "passwd").exists()

    def test_rejects_absolute_filename(self, tmp_path: Path) -> None:
        """An absolute filename must be rejected."""
        mgr = ImageManager(cache_dir=tmp_path)
        with pytest.raises(ValueError, match="relative paths"):
            mgr.ensure_rootfs_only(
                "evil-image",
                url="https://example.com/rootfs.qcow2",
                filename="/tmp/escape.qcow2",
            )

    def test_sha512_mismatch_deletes_file_and_raises(self, tmp_path: Path) -> None:
        """A SHA-512 mismatch should delete the downloaded file and raise."""
        mgr = ImageManager(cache_dir=tmp_path)

        def fake_download(
            self: ImageManager,
            url: str,
            dest: Path,
            expected_sha256: str | None = None,
            *,
            progress_callback: object = None,
        ) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"corrupt")

        ctx = patch.object(ImageManager, "_download_file", fake_download)
        with ctx, pytest.raises(ImageError, match="SHA-512 mismatch"):
            mgr.ensure_rootfs_only(
                "bad-image",
                url="https://example.com/rootfs.qcow2",
                sha512="0" * 128,
            )

        # The file must be deleted so retries don't return a bad cache.
        assert not (tmp_path / "bad-image" / "rootfs.qcow2").exists()

    def test_sha512_match_caches_file(self, tmp_path: Path) -> None:
        """A valid SHA-512 should pass and the file should be cached."""
        import hashlib

        content = b"hello world"
        expected_sha512 = hashlib.sha512(content).hexdigest()
        mgr = ImageManager(cache_dir=tmp_path)

        def fake_download(
            self: ImageManager,
            url: str,
            dest: Path,
            expected_sha256: str | None = None,
            *,
            progress_callback: object = None,
        ) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

        with patch.object(ImageManager, "_download_file", fake_download):
            result = mgr.ensure_rootfs_only(
                "good-image",
                url="https://example.com/rootfs.qcow2",
                sha512=expected_sha512,
            )

        assert result.exists()
        assert result.read_bytes() == content


class TestImageManagerInit:
    """Tests for ImageManager initialization."""

    def test_custom_cache_dir(self, tmp_path: Path) -> None:
        """Test custom cache directory."""
        mgr = ImageManager(cache_dir=tmp_path / "custom")
        assert mgr.cache_dir == tmp_path / "custom"

    def test_custom_registry(self, tmp_path: Path) -> None:
        """Test custom registry overrides built-in."""
        registry: dict[str, ImageSource] = {}
        mgr = ImageManager(cache_dir=tmp_path, registry=registry)
        assert mgr.list_available() == []

    def test_default_registry_is_empty_after_kernel_migration(self) -> None:
        """Default BUILTIN_IMAGES is intentionally empty post-0.0.14a0.

        SmolVM ships its kernel via ``smolvm.images.published.BASE_KERNELS``
        and rootfs via ``MANIFEST`` there. The legacy demo entries
        (``hello``, ``quickstart-x86_64``) pointed at retired Firecracker S3
        URLs and have been removed.
        """
        mgr = ImageManager()
        assert mgr.list_available() == []


# -----------------------------------------------------------------------
# S3 image support
# -----------------------------------------------------------------------


class TestParseS3ImageUri:
    """Tests for S3 URI parsing."""

    def test_valid_uri(self) -> None:
        ref = parse_s3_image_uri("s3://my-bucket/images/alpine-ssh/")
        assert ref == S3ImageRef(bucket="my-bucket", prefix="images/alpine-ssh")

    def test_valid_uri_no_trailing_slash(self) -> None:
        ref = parse_s3_image_uri("s3://my-bucket/images/alpine-ssh")
        assert ref.bucket == "my-bucket"
        assert ref.prefix == "images/alpine-ssh"

    def test_single_level_prefix(self) -> None:
        ref = parse_s3_image_uri("s3://bucket/image")
        assert ref.bucket == "bucket"
        assert ref.prefix == "image"

    def test_wrong_scheme_raises(self) -> None:
        with pytest.raises(ImageError, match="Expected an s3:// URI"):
            parse_s3_image_uri("https://bucket/key")

    def test_missing_bucket_raises(self) -> None:
        with pytest.raises(ImageError, match="missing a bucket"):
            parse_s3_image_uri("s3:///just-a-key")

    def test_missing_prefix_raises(self) -> None:
        with pytest.raises(ImageError, match="missing an image prefix"):
            parse_s3_image_uri("s3://bucket-only")

    def test_missing_prefix_trailing_slash_raises(self) -> None:
        with pytest.raises(ImageError, match="missing an image prefix"):
            parse_s3_image_uri("s3://bucket-only/")


class TestS3ImageManifest:
    """Tests for S3 manifest model validation."""

    def test_valid_manifest(self) -> None:
        manifest = S3ImageManifest(
            name="alpine-ssh",
            kernel="vmlinux.bin",
            kernel_sha256="abc123",
            rootfs="rootfs.ext4",
            rootfs_sha256="def456",
        )
        assert manifest.name == "alpine-ssh"
        assert manifest.initrd is None
        assert manifest.boot_args is None

    def test_manifest_with_initrd(self) -> None:
        manifest = S3ImageManifest(
            name="ubuntu",
            kernel="vmlinuz",
            rootfs="rootfs.qcow2",
            initrd="initrd.img",
            initrd_sha256="aaa",
        )
        assert manifest.initrd == "initrd.img"

    def test_manifest_with_boot_args(self) -> None:
        manifest = S3ImageManifest(
            name="custom",
            kernel="vmlinux",
            rootfs="rootfs.ext4",
            boot_args="console=ttyS0 root=/dev/vda rw",
        )
        assert manifest.boot_args == "console=ttyS0 root=/dev/vda rw"

    def test_manifest_missing_required_field(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            S3ImageManifest(name="bad", kernel="k")  # type: ignore[call-arg]

    def test_manifest_rejects_absolute_path(self) -> None:
        with pytest.raises(ValueError, match="must be relative"):
            S3ImageManifest(name="bad", kernel="/etc/passwd", rootfs="rootfs.ext4")

    def test_manifest_rejects_path_traversal(self) -> None:
        with pytest.raises(ValueError, match="must not contain"):
            S3ImageManifest(name="bad", kernel="../../.ssh/keys", rootfs="rootfs.ext4")

    def test_manifest_normalizes_nested_path_to_basename(self) -> None:
        m = S3ImageManifest(name="ok", kernel="nested/vmlinux.bin", rootfs="rootfs.ext4")
        assert m.kernel == "vmlinux.bin"

    def test_manifest_rejects_colliding_filenames(self) -> None:
        with pytest.raises(ValueError, match="collide"):
            S3ImageManifest(name="bad", kernel="same.bin", rootfs="same.bin")

    def test_manifest_rejects_colliding_filenames_with_initrd(self) -> None:
        with pytest.raises(ValueError, match="collide"):
            S3ImageManifest(name="bad", kernel="vmlinux", rootfs="rootfs.ext4", initrd="vmlinux")


def _make_s3_manifest_data(
    *,
    name: str = "test-image",
    kernel: str = "vmlinux.bin",
    rootfs: str = "rootfs.ext4",
    kernel_sha256: str | None = None,
    rootfs_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "kernel": kernel,
        "kernel_sha256": kernel_sha256,
        "rootfs": rootfs,
        "rootfs_sha256": rootfs_sha256,
    }


class TestEnsureS3Image:
    """Tests for S3 image download and caching."""

    def _mock_s3_client(
        self,
        manifest_data: dict[str, object],
        assets: dict[str, bytes],
        *,
        expected_bucket: str = "bucket",
    ) -> MagicMock:
        """Create a mock boto3 S3 client that serves a manifest + assets."""
        client = MagicMock()
        manifest_json = json.dumps(manifest_data).encode()

        def get_object(Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            assert Bucket == expected_bucket, f"unexpected bucket: {Bucket}"
            filename = Key.rsplit("/", 1)[-1]
            if filename == "smolvm-image.json":
                content = manifest_json
            elif filename in assets:
                content = assets[filename]
            else:
                raise Exception(f"unexpected S3 key: {Key}")
            body = MagicMock()
            body.iter_chunks.return_value = iter([content])
            return {"Body": body}

        client.get_object.side_effect = get_object
        return client

    def test_downloads_and_caches(self, tmp_path: Path) -> None:
        """First call should download all assets from S3."""
        kernel_content = b"fake-kernel"
        rootfs_content = b"fake-rootfs"
        manifest = _make_s3_manifest_data(
            kernel_sha256=hashlib.sha256(kernel_content).hexdigest(),
            rootfs_sha256=hashlib.sha256(rootfs_content).hexdigest(),
        )
        mock_s3 = self._mock_s3_client(
            manifest,
            {
                "vmlinux.bin": kernel_content,
                "rootfs.ext4": rootfs_content,
            },
        )

        mgr = ImageManager(cache_dir=tmp_path / "images")
        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            local, parsed_manifest = mgr.ensure_s3_image("s3://bucket/images/test/")

        assert local.name == "test-image"
        assert local.kernel_path.exists()
        assert local.rootfs_path.exists()
        assert local.kernel_path.read_bytes() == kernel_content
        assert local.rootfs_path.read_bytes() == rootfs_content
        assert parsed_manifest.name == "test-image"

    def test_cache_hit_skips_download(self, tmp_path: Path) -> None:
        """Second call should use cache and not re-download assets."""
        kernel_content = b"fake-kernel"
        rootfs_content = b"fake-rootfs"
        manifest = _make_s3_manifest_data(
            kernel_sha256=hashlib.sha256(kernel_content).hexdigest(),
            rootfs_sha256=hashlib.sha256(rootfs_content).hexdigest(),
        )
        mock_s3 = self._mock_s3_client(
            manifest,
            {
                "vmlinux.bin": kernel_content,
                "rootfs.ext4": rootfs_content,
            },
        )

        mgr = ImageManager(cache_dir=tmp_path / "images")

        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            # First call downloads
            mgr.ensure_s3_image("s3://bucket/images/test/")
            # Reset mock call count
            mock_s3.get_object.reset_mock()
            # Second call should use cache
            local, _ = mgr.ensure_s3_image("s3://bucket/images/test/")

        assert local.kernel_path.exists()
        # Only manifest was re-fetched; assets came from cache
        assert mock_s3.get_object.call_count == 1

    def test_sha_mismatch_re_downloads(self, tmp_path: Path) -> None:
        """Corrupted cache should trigger re-download."""
        kernel_content = b"fake-kernel"
        rootfs_content = b"fake-rootfs"
        manifest = _make_s3_manifest_data(
            kernel_sha256=hashlib.sha256(kernel_content).hexdigest(),
            rootfs_sha256=hashlib.sha256(rootfs_content).hexdigest(),
        )
        mock_s3 = self._mock_s3_client(
            manifest,
            {
                "vmlinux.bin": kernel_content,
                "rootfs.ext4": rootfs_content,
            },
        )

        mgr = ImageManager(cache_dir=tmp_path / "images")

        # First download to populate cache
        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            local, _ = mgr.ensure_s3_image("s3://bucket/images/test/")

        # Corrupt the cached kernel
        local.kernel_path.write_bytes(b"corrupted")

        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            mock_s3.get_object.reset_mock()
            local2, _ = mgr.ensure_s3_image("s3://bucket/images/test/")

        # Kernel should have been re-downloaded (but rootfs cache hit)
        # Manifest + 1 re-downloaded asset = 2 get_object calls
        assert mock_s3.get_object.call_count == 2
        assert local2.kernel_path.read_bytes() == kernel_content

    def test_no_sha_still_caches(self, tmp_path: Path) -> None:
        """Images without SHA-256 hashes should still cache and return."""
        manifest = _make_s3_manifest_data()  # no sha256 fields
        mock_s3 = self._mock_s3_client(
            manifest,
            {
                "vmlinux.bin": b"kernel",
                "rootfs.ext4": b"rootfs",
            },
        )

        mgr = ImageManager(cache_dir=tmp_path / "images")
        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            local, _ = mgr.ensure_s3_image("s3://bucket/images/nosha/")

        assert local.kernel_path.exists()
        assert local.rootfs_path.exists()

    def test_manifest_download_failure_raises(self, tmp_path: Path) -> None:
        """Failed manifest download should raise ImageError."""
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("Access Denied")

        mgr = ImageManager(cache_dir=tmp_path / "images")
        with (
            patch("smolvm.images.manager._require_boto3", return_value=mock_s3),
            pytest.raises(ImageError, match="Failed to download image manifest"),
        ):
            mgr.ensure_s3_image("s3://bucket/images/private/")

    def test_invalid_manifest_json_raises(self, tmp_path: Path) -> None:
        """Malformed manifest JSON should raise ImageError."""
        mock_s3 = MagicMock()
        body = MagicMock()
        body.iter_chunks.return_value = iter([b"not-json{{{"])
        mock_s3.get_object.return_value = {"Body": body}

        mgr = ImageManager(cache_dir=tmp_path / "images")
        with (
            patch("smolvm.images.manager._require_boto3", return_value=mock_s3),
            pytest.raises(ImageError, match="Invalid smolvm-image.json"),
        ):
            mgr.ensure_s3_image("s3://bucket/images/bad/")

    def test_missing_boto3_raises_helpful_error(self, tmp_path: Path) -> None:
        """Missing boto3 should produce a clear installation hint."""
        mgr = ImageManager(cache_dir=tmp_path / "images")
        with (
            patch(
                "smolvm.images.manager._require_boto3",
                side_effect=ImageError("S3 image support requires boto3"),
            ),
            pytest.raises(ImageError, match="requires boto3"),
        ):
            mgr.ensure_s3_image("s3://bucket/images/test/")

    def test_with_initrd(self, tmp_path: Path) -> None:
        """S3 images with an initrd should download all three assets."""
        kernel_content = b"kernel"
        rootfs_content = b"rootfs"
        initrd_content = b"initrd"
        manifest_data = {
            "name": "with-initrd",
            "kernel": "vmlinuz",
            "rootfs": "rootfs.qcow2",
            "initrd": "initrd.img",
            "kernel_sha256": hashlib.sha256(kernel_content).hexdigest(),
            "rootfs_sha256": hashlib.sha256(rootfs_content).hexdigest(),
            "initrd_sha256": hashlib.sha256(initrd_content).hexdigest(),
        }
        mock_s3 = self._mock_s3_client(
            manifest_data,
            {
                "vmlinuz": kernel_content,
                "rootfs.qcow2": rootfs_content,
                "initrd.img": initrd_content,
            },
        )

        mgr = ImageManager(cache_dir=tmp_path / "images")
        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            local, manifest = mgr.ensure_s3_image("s3://bucket/images/ubuntu/")

        assert local.initrd_path is not None
        assert local.initrd_path.exists()
        assert local.initrd_path.read_bytes() == initrd_content
        assert manifest.initrd == "initrd.img"

    def test_offline_cache_fallback(self, tmp_path: Path) -> None:
        """Fully cached image should work when S3 is unreachable."""
        kernel_content = b"fake-kernel"
        rootfs_content = b"fake-rootfs"
        manifest = _make_s3_manifest_data(
            kernel_sha256=hashlib.sha256(kernel_content).hexdigest(),
            rootfs_sha256=hashlib.sha256(rootfs_content).hexdigest(),
        )
        mock_s3 = self._mock_s3_client(
            manifest,
            {
                "vmlinux.bin": kernel_content,
                "rootfs.ext4": rootfs_content,
            },
        )

        mgr = ImageManager(cache_dir=tmp_path / "images")

        # First call — populate cache
        with patch("smolvm.images.manager._require_boto3", return_value=mock_s3):
            mgr.ensure_s3_image("s3://bucket/images/test/")

        # Second call — S3 is down, should use cached manifest + assets
        offline_s3 = MagicMock()
        offline_s3.get_object.side_effect = Exception("Network unreachable")

        with patch("smolvm.images.manager._require_boto3", return_value=offline_s3):
            local, _ = mgr.ensure_s3_image("s3://bucket/images/test/")

        assert local.kernel_path.exists()
        assert local.rootfs_path.exists()
        assert local.kernel_path.read_bytes() == kernel_content


class TestS3CredentialResolution:
    """Tests for SMOLVM_S3_* env var resolution."""

    _TEST_ACCESS_KEY = "test-access-key-id"  # noqa: S105
    _TEST_SECRET_KEY = "test-secret-access-key"  # noqa: S105

    def test_smolvm_env_vars_override_defaults(self) -> None:
        """SMOLVM_S3_* vars should be passed to boto3.client()."""
        import sys

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()

        env = {
            "SMOLVM_S3_ENDPOINT_URL": "https://custom.endpoint.example",
            "SMOLVM_S3_ACCESS_KEY_ID": self._TEST_ACCESS_KEY,
            "SMOLVM_S3_SECRET_ACCESS_KEY": self._TEST_SECRET_KEY,
        }
        with patch.dict("os.environ", env), patch.dict(sys.modules, {"boto3": mock_boto3}):
            from smolvm.images.manager import _require_boto3

            _require_boto3()

        mock_boto3.client.assert_called_once_with(
            "s3",
            endpoint_url="https://custom.endpoint.example",
            region_name="auto",
            aws_access_key_id=self._TEST_ACCESS_KEY,
            aws_secret_access_key=self._TEST_SECRET_KEY,
        )

    def test_endpoint_only_uses_boto3_cred_chain(self) -> None:
        """When only endpoint is set, credentials fall back to boto3 chain."""
        import os
        import sys

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        mock_dotenv = MagicMock()

        env = {"SMOLVM_S3_ENDPOINT_URL": "https://r2.example.com"}
        with (
            patch.dict("os.environ", env),
            patch.dict(sys.modules, {"boto3": mock_boto3, "dotenv": mock_dotenv}),
        ):
            os.environ.pop("SMOLVM_S3_ACCESS_KEY_ID", None)
            os.environ.pop("SMOLVM_S3_SECRET_ACCESS_KEY", None)

            from smolvm.images.manager import _require_boto3

            _require_boto3()

        mock_boto3.client.assert_called_once_with(
            "s3",
            endpoint_url="https://r2.example.com",
            region_name="auto",
        )

    def test_no_smolvm_vars_uses_plain_boto3(self) -> None:
        """Without SMOLVM_S3_* vars, boto3 defaults should be used."""
        import os
        import sys

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = MagicMock()
        mock_dotenv = MagicMock()

        with (
            patch.dict("os.environ", {}, clear=False),
            patch.dict(sys.modules, {"boto3": mock_boto3, "dotenv": mock_dotenv}),
        ):
            os.environ.pop("SMOLVM_S3_ENDPOINT_URL", None)
            os.environ.pop("SMOLVM_S3_ACCESS_KEY_ID", None)
            os.environ.pop("SMOLVM_S3_SECRET_ACCESS_KEY", None)

            from smolvm.images.manager import _require_boto3

            _require_boto3()

        mock_boto3.client.assert_called_once_with("s3")

    def test_half_set_credentials_raises(self) -> None:
        """Setting only access key without secret should raise."""
        import sys

        mock_boto3 = MagicMock()
        mock_dotenv = MagicMock()

        env = {"SMOLVM_S3_ACCESS_KEY_ID": "only-key"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch.dict(sys.modules, {"boto3": mock_boto3, "dotenv": mock_dotenv}),
            pytest.raises(ImageError, match="Incomplete S3 credentials"),
        ):
            import os

            os.environ.pop("SMOLVM_S3_SECRET_ACCESS_KEY", None)
            os.environ.pop("SMOLVM_S3_ENDPOINT_URL", None)

            from smolvm.images.manager import _require_boto3

            _require_boto3()
