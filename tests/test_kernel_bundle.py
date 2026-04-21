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

"""Tests for ImageManager.ensure_kernel_bundle + KernelBundle."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import zstandard
from pydantic import ValidationError

from smolvm.exceptions import ImageError
from smolvm.images.manager import ImageManager, KernelBundle

# ---------------------------------------------------------------------------
# Helpers — build realistic .tar.zst bundles in-memory for tests.
# ---------------------------------------------------------------------------


def _make_bundle_bytes(
    *,
    kernel_filename: str = "vmlinux",
    kernel_content: bytes = b"fake-kernel-binary-content",
    include_config: bool = True,
    include_linux_version: bool = True,
    linux_version_content: str = "6.12.82",
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    """Build an in-memory ``.tar.zst`` bundle matching our release layout.

    The defaults produce a well-formed bundle; override the kwargs to
    test edge cases (missing files, wrong kernel name, etc.).
    """
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:

        def _add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        _add(kernel_filename, kernel_content)
        if include_config:
            _add("config", b"# fake kernel config\nCONFIG_VIRTIO_FS=y\n")
        if include_linux_version:
            _add("LINUX_VERSION", linux_version_content.encode() + b"\n")
        _add("SHA256SUMS", b"aaaa  " + kernel_filename.encode() + b"\n")
        for name, data in (extra_files or {}).items():
            _add(name, data)

    cctx = zstandard.ZstdCompressor()
    return cctx.compress(tar_buf.getvalue())


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def image_manager(tmp_path: Path) -> ImageManager:
    """Fresh ImageManager with an isolated cache dir per test."""
    return ImageManager(cache_dir=tmp_path / "cache")


def _mock_requests_get_returning(body: bytes) -> MagicMock:
    """Build a requests.get mock that streams ``body`` once."""

    def _factory(*args: object, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {"content-length": str(len(body))}
        resp.iter_content = lambda chunk_size: iter([body])
        return resp

    mock = MagicMock(side_effect=_factory)
    return mock


# ---------------------------------------------------------------------------
# KernelBundle model
# ---------------------------------------------------------------------------


class TestKernelBundle:
    def test_has_expected_fields(self) -> None:
        bundle = KernelBundle(
            kernel_path=Path("/tmp/vmlinux"),
            config_path=Path("/tmp/config"),
            linux_version="6.12.82",
            bundle_sha256="a" * 64,
        )
        assert bundle.kernel_path == Path("/tmp/vmlinux")
        assert bundle.config_path == Path("/tmp/config")
        assert bundle.linux_version == "6.12.82"
        assert bundle.bundle_sha256 == "a" * 64

    def test_is_frozen(self) -> None:
        bundle = KernelBundle(
            kernel_path=Path("/tmp/vmlinux"),
            config_path=Path("/tmp/config"),
            linux_version="6.12.82",
            bundle_sha256="a" * 64,
        )
        with pytest.raises(ValidationError):
            bundle.linux_version = "6.12.83"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Input validation — ensure_kernel_bundle refuses unsafe inputs early.
# ---------------------------------------------------------------------------


class TestEnsureKernelBundleInputValidation:
    def test_empty_sha256_raises(self, image_manager: ImageManager) -> None:
        with pytest.raises(ValueError, match="non-empty bundle_sha256"):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256="",
                cache_key="k",
                expected_kernel_filename="vmlinux",
            )

    def test_empty_cache_key_raises(self, image_manager: ImageManager) -> None:
        with pytest.raises(ValueError, match="cache_key cannot be empty"):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256="a" * 64,
                cache_key="",
                expected_kernel_filename="vmlinux",
            )

    def test_bad_kernel_filename_raises(self, image_manager: ImageManager) -> None:
        with pytest.raises(ValueError, match="must be 'vmlinux' or 'Image'"):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256="a" * 64,
                cache_key="k",
                expected_kernel_filename="bzImage",
            )


# ---------------------------------------------------------------------------
# Happy path + caching.
# ---------------------------------------------------------------------------


class TestEnsureKernelBundleHappyPath:
    def test_downloads_extracts_and_returns_bundle(self, image_manager: ImageManager) -> None:
        body = _make_bundle_bytes()
        sha = _sha256_of(body)

        with patch(
            "smolvm.images.manager.requests.get",
            _mock_requests_get_returning(body),
        ):
            bundle = image_manager.ensure_kernel_bundle(
                url="https://example.invalid/smolvm-kernel-v1.0.0-x86_64.tar.zst",
                bundle_sha256=sha,
                cache_key="kernel-v1.0.0-x86_64",
                expected_kernel_filename="vmlinux",
            )

        assert isinstance(bundle, KernelBundle)
        assert bundle.kernel_path.is_file()
        assert bundle.kernel_path.name == "vmlinux"
        assert bundle.config_path.is_file()
        assert bundle.linux_version == "6.12.82"
        assert bundle.bundle_sha256 == sha

    def test_handles_aarch64_Image_filename(  # noqa: N802 — Image is the aarch64 kernel filename
        self, image_manager: ImageManager
    ) -> None:
        body = _make_bundle_bytes(kernel_filename="Image")
        sha = _sha256_of(body)

        with patch(
            "smolvm.images.manager.requests.get",
            _mock_requests_get_returning(body),
        ):
            bundle = image_manager.ensure_kernel_bundle(
                url="https://example.invalid/smolvm-kernel-v1.0.0-aarch64.tar.zst",
                bundle_sha256=sha,
                cache_key="kernel-v1.0.0-aarch64",
                expected_kernel_filename="Image",
            )

        assert bundle.kernel_path.name == "Image"

    def test_second_call_is_cache_hit(self, image_manager: ImageManager) -> None:
        body = _make_bundle_bytes()
        sha = _sha256_of(body)
        url = "https://example.invalid/smolvm-kernel-v1.0.0-x86_64.tar.zst"

        mock = _mock_requests_get_returning(body)
        with patch("smolvm.images.manager.requests.get", mock):
            image_manager.ensure_kernel_bundle(
                url=url,
                bundle_sha256=sha,
                cache_key="kernel-v1.0.0-x86_64",
                expected_kernel_filename="vmlinux",
            )
            first_call_count = mock.call_count

            # Second call with identical params must NOT re-fetch.
            bundle = image_manager.ensure_kernel_bundle(
                url=url,
                bundle_sha256=sha,
                cache_key="kernel-v1.0.0-x86_64",
                expected_kernel_filename="vmlinux",
            )
            assert mock.call_count == first_call_count
            assert bundle.kernel_path.is_file()

    def test_sha_bump_invalidates_cache(self, image_manager: ImageManager) -> None:
        """A new sha (e.g. bundle version bump) must trigger a re-fetch
        even if the old bundle is still on disk."""
        body_v1 = _make_bundle_bytes(kernel_content=b"kernel-v1")
        body_v2 = _make_bundle_bytes(kernel_content=b"kernel-v2")
        sha_v1 = _sha256_of(body_v1)
        sha_v2 = _sha256_of(body_v2)

        cache_key = "kernel-v1.0.0-x86_64"

        with patch(
            "smolvm.images.manager.requests.get",
            _mock_requests_get_returning(body_v1),
        ):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/v1.tar.zst",
                bundle_sha256=sha_v1,
                cache_key=cache_key,
                expected_kernel_filename="vmlinux",
            )

        with patch(
            "smolvm.images.manager.requests.get",
            _mock_requests_get_returning(body_v2),
        ):
            bundle = image_manager.ensure_kernel_bundle(
                url="https://example.invalid/v2.tar.zst",
                bundle_sha256=sha_v2,
                cache_key=cache_key,
                expected_kernel_filename="vmlinux",
            )

        assert bundle.kernel_path.read_bytes() == b"kernel-v2"
        assert bundle.bundle_sha256 == sha_v2


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------


class TestEnsureKernelBundleFailureModes:
    def test_sha_mismatch_raises_and_cleans_up(self, image_manager: ImageManager) -> None:
        body = _make_bundle_bytes()
        wrong_sha = "0" * 64

        with (
            patch(
                "smolvm.images.manager.requests.get",
                _mock_requests_get_returning(body),
            ),
            pytest.raises(ImageError, match="SHA-256 mismatch"),
        ):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256=wrong_sha,
                cache_key="mismatch-case",
                expected_kernel_filename="vmlinux",
            )

    def test_missing_kernel_raises(self, image_manager: ImageManager) -> None:
        """Bundle extracts but contains only non-kernel filename."""
        body = _make_bundle_bytes(kernel_filename="wrong-name.bin")
        sha = _sha256_of(body)

        with (
            patch(
                "smolvm.images.manager.requests.get",
                _mock_requests_get_returning(body),
            ),
            pytest.raises(ImageError, match="missing required file: vmlinux"),
        ):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256=sha,
                cache_key="missing-kernel",
                expected_kernel_filename="vmlinux",
            )

    def test_missing_config_raises(self, image_manager: ImageManager) -> None:
        body = _make_bundle_bytes(include_config=False)
        sha = _sha256_of(body)

        with (
            patch(
                "smolvm.images.manager.requests.get",
                _mock_requests_get_returning(body),
            ),
            pytest.raises(ImageError, match="missing required file: config"),
        ):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256=sha,
                cache_key="missing-config",
                expected_kernel_filename="vmlinux",
            )

    def test_missing_linux_version_raises(self, image_manager: ImageManager) -> None:
        body = _make_bundle_bytes(include_linux_version=False)
        sha = _sha256_of(body)

        with (
            patch(
                "smolvm.images.manager.requests.get",
                _mock_requests_get_returning(body),
            ),
            pytest.raises(ImageError, match="missing required file: LINUX_VERSION"),
        ):
            image_manager.ensure_kernel_bundle(
                url="https://example.invalid/b.tar.zst",
                bundle_sha256=sha,
                cache_key="missing-linux-version",
                expected_kernel_filename="vmlinux",
            )
