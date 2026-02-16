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

"""Image management for SmolVM.

Handles fetching, caching, and validating VM assets (kernels, rootfs).
"""

import hashlib
import logging
import tempfile
from pathlib import Path

import requests
from pydantic import BaseModel

from smolvm.exceptions import ImageError

logger = logging.getLogger(__name__)

# Chunk size for streaming downloads (8 KB)
_DOWNLOAD_CHUNK_SIZE = 8192


class ImageSource(BaseModel):
    """Definition of a downloadable VM image.

    Attributes:
        name: Human-readable image name.
        kernel_url: URL to download the kernel binary.
        kernel_sha256: Expected SHA-256 hex digest of the kernel,
            or None to skip verification.
        rootfs_url: URL to download the root filesystem.
        rootfs_sha256: Expected SHA-256 hex digest of the rootfs,
            or None to skip verification.
    """

    name: str
    kernel_url: str
    kernel_sha256: str | None = None
    rootfs_url: str
    rootfs_sha256: str | None = None

    model_config = {"frozen": True}


class LocalImage(BaseModel):
    """A locally-cached VM image ready for use.

    Attributes:
        name: Image name.
        kernel_path: Absolute path to the kernel binary.
        rootfs_path: Absolute path to the root filesystem.
    """

    name: str
    kernel_path: Path
    rootfs_path: Path

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Built-in image registry
#
# Version-pinned URLs from Firecracker's official CI/quickstart assets.
# To add a new image, append an ImageSource entry here.
# ---------------------------------------------------------------------------
BUILTIN_IMAGES: dict[str, ImageSource] = {
    "hello": ImageSource(
        name="hello",
        kernel_url=("https://s3.amazonaws.com/spec.ccfc.min/img/hello/kernel/hello-vmlinux.bin"),
        rootfs_url=("https://s3.amazonaws.com/spec.ccfc.min/img/hello/fsfiles/hello-rootfs.ext4"),
    ),
    "quickstart-x86_64": ImageSource(
        name="quickstart-x86_64",
        kernel_url=(
            "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin"
        ),
        rootfs_url=(
            "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide"
            "/x86_64/rootfs/bionic.rootfs.ext4"
        ),
    ),
}


class ImageManager:
    """Manages VM image downloads, caching, and verification.

    Images are cached under ``~/.smolvm/images/<name>/``.
    Downloads are atomic: written to a temporary file, SHA-256 verified,
    then renamed into place so a partial download never corrupts the cache.
    """

    DEFAULT_CACHE_DIR = Path.home() / ".smolvm" / "images"

    def __init__(
        self,
        cache_dir: Path | None = None,
        registry: dict[str, ImageSource] | None = None,
    ) -> None:
        """Initialize the image manager.

        Args:
            cache_dir: Override the default cache directory.
            registry: Override the built-in image registry.
                Useful for testing or adding custom images.
        """
        self.cache_dir = cache_dir or self.DEFAULT_CACHE_DIR
        self.registry = registry if registry is not None else dict(BUILTIN_IMAGES)

    def list_available(self) -> list[str]:
        """List names of all registered images.

        Returns:
            Sorted list of image names.
        """
        return sorted(self.registry.keys())

    def is_cached(self, name: str) -> bool:
        """Check if an image is fully cached locally.

        Args:
            name: Image name.

        Returns:
            True if both kernel and rootfs files exist in the cache.
        """
        if not name:
            raise ValueError("image name cannot be empty")

        image_dir = self.cache_dir / name
        kernel = image_dir / "vmlinux.bin"
        rootfs = image_dir / "rootfs.ext4"

        return kernel.is_file() and rootfs.is_file()

    def ensure_image(self, name: str) -> LocalImage:
        """Ensure an image is available locally, downloading if necessary.

        If the image is already cached and passes SHA-256 verification,
        it is returned immediately. Otherwise, it is downloaded.

        Args:
            name: Image name from the registry.

        Returns:
            LocalImage with paths to kernel and rootfs.

        Raises:
            ImageError: If the image is not in the registry,
                download fails, or checksum verification fails.
        """
        if not name:
            raise ValueError("image name cannot be empty")

        source = self.registry.get(name)
        if source is None:
            available = ", ".join(self.list_available()) or "(none)"
            raise ImageError(f"Unknown image: '{name}'. Available images: {available}")

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        # Check cache — re-download if SHA mismatch
        if kernel_path.is_file() and rootfs_path.is_file():
            kernel_ok = self._verify_sha256(kernel_path, source.kernel_sha256)
            rootfs_ok = self._verify_sha256(rootfs_path, source.rootfs_sha256)
            if kernel_ok and rootfs_ok:
                logger.info("Image '%s' found in cache: %s", name, image_dir)
                return LocalImage(
                    name=name,
                    kernel_path=kernel_path,
                    rootfs_path=rootfs_path,
                )
            logger.warning("Cached image '%s' failed SHA-256 check, re-downloading", name)

        # Download
        image_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading image '%s' kernel...", name)
        self._download_file(source.kernel_url, kernel_path, source.kernel_sha256)

        logger.info("Downloading image '%s' rootfs...", name)
        self._download_file(source.rootfs_url, rootfs_path, source.rootfs_sha256)

        logger.info("Image '%s' ready at: %s", name, image_dir)
        return LocalImage(
            name=name,
            kernel_path=kernel_path,
            rootfs_path=rootfs_path,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _download_file(self, url: str, dest: Path, expected_sha256: str | None = None) -> None:
        """Download a file with atomic write and optional SHA-256 verification.

        The file is written to a temporary location. If a checksum is
        provided and matches, it is renamed into place. When
        ``expected_sha256`` is None, verification is skipped.

        Args:
            url: URL to download from.
            dest: Final destination path.
            expected_sha256: Expected SHA-256 hex digest, or None to
                skip verification.

        Raises:
            ImageError: If download or checksum verification fails.
        """
        # Use a temp file in the same directory for atomic rename
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        tmp_path = Path(tmp_path_str)

        try:
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()

            sha256 = hashlib.sha256() if expected_sha256 else None
            with open(tmp_fd, "wb") as f:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    f.write(chunk)
                    if sha256 is not None:
                        sha256.update(chunk)

            if sha256 is not None and expected_sha256 is not None:
                actual_hash = sha256.hexdigest()
                if actual_hash != expected_sha256:
                    raise ImageError(
                        f"SHA-256 mismatch for {url}\n"
                        f"  expected: {expected_sha256}\n"
                        f"  actual:   {actual_hash}"
                    )

            # Atomic rename
            tmp_path.rename(dest)
            logger.debug("Downloaded %s -> %s", url, dest)

        except requests.RequestException as e:
            raise ImageError(f"Download failed for {url}: {e}") from e
        finally:
            # Clean up temp file on failure
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _verify_sha256(path: Path, expected: str | None) -> bool:
        """Verify the SHA-256 checksum of a file.

        Args:
            path: Path to the file.
            expected: Expected SHA-256 hex digest, or None to skip.

        Returns:
            True if the checksum matches or if no hash was provided.
        """
        if expected is None:
            return True

        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)

        actual = sha256.hexdigest()
        if actual != expected:
            logger.debug(
                "SHA-256 mismatch for %s: expected=%s, actual=%s",
                path,
                expected,
                actual,
            )
            return False
        return True
