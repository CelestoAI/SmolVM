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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.images import ImageManager, ImageSource, LocalImage


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

        with patch("smolvm.images.requests.get") as mock_get:
            result = image_manager.ensure_image("test-image")

            # Should NOT have downloaded anything
            mock_get.assert_not_called()

        assert isinstance(result, LocalImage)
        assert result.name == "test-image"
        assert result.kernel_path.exists()
        assert result.rootfs_path.exists()

    @patch("smolvm.images.requests.get")
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

    @patch("smolvm.images.requests.get")
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

    @patch("smolvm.images.requests.get")
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

    @patch("smolvm.images.requests.get")
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

    @patch("smolvm.images.requests.get")
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

        with patch("smolvm.images.requests.get") as mock_get:
            result = mgr.ensure_image("no-sha")
            mock_get.assert_not_called()

        assert result.name == "no-sha"
        assert result.kernel_path.exists()


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

    def test_default_has_builtin_images(self) -> None:
        """Test default registry has built-in images."""
        mgr = ImageManager()
        names = mgr.list_available()
        assert "hello" in names
