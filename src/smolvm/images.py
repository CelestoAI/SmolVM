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
Supports both HTTP URLs and S3-hosted images via an optional ``boto3``
dependency (install with ``pip install 'smolvm[s3]'``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, field_validator, model_validator

from smolvm.exceptions import ImageError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

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
        kernel_filename: Local cache filename for the kernel.
        initrd_url: Optional URL to download an initrd.
        initrd_sha256: Expected SHA-256 digest for the initrd, or None.
        initrd_filename: Local cache filename for the initrd.
        rootfs_url: URL to download the root filesystem.
        rootfs_sha256: Expected SHA-256 hex digest of the rootfs,
            or None to skip verification.
        rootfs_filename: Local cache filename for the rootfs.
    """

    name: str
    kernel_url: str
    kernel_sha256: str | None = None
    kernel_filename: str = "vmlinux.bin"
    initrd_url: str | None = None
    initrd_sha256: str | None = None
    initrd_filename: str = "initrd.img"
    rootfs_url: str
    rootfs_sha256: str | None = None
    rootfs_filename: str = "rootfs.ext4"

    @field_validator("kernel_filename", "initrd_filename", "rootfs_filename")
    @classmethod
    def normalize_cache_filename(cls, value: str) -> str:
        """Normalize cache filenames to safe basenames."""
        raw_path = Path(value)
        if raw_path.is_absolute():
            raise ValueError("image cache filenames must be relative paths")

        normalized = raw_path.name
        if not normalized or normalized in {".", ".."}:
            raise ValueError("image cache filenames must resolve to a basename")
        return normalized

    model_config = {"frozen": True}


class LocalImage(BaseModel):
    """A locally-cached VM image ready for use.

    Attributes:
        name: Image name.
        kernel_path: Absolute path to the kernel binary.
        initrd_path: Optional absolute path to the initrd.
        rootfs_path: Absolute path to the root filesystem.
    """

    name: str
    kernel_path: Path
    initrd_path: Path | None = None
    rootfs_path: Path

    model_config = {"frozen": True}


class S3ImageRef(BaseModel):
    """Parsed S3 image URI.

    Attributes:
        bucket: S3 bucket name.
        prefix: Object key prefix (without trailing ``/``).
        region: Optional AWS region override.
    """

    bucket: str
    prefix: str
    region: str | None = None

    model_config = {"frozen": True}


class S3ImageManifest(BaseModel):
    """Schema for the ``smolvm-image.json`` manifest stored alongside
    image assets in S3.

    Attributes:
        name: Human-readable image name.
        kernel: Kernel filename relative to the manifest prefix.
        kernel_sha256: Expected SHA-256 hex digest of the kernel, or None.
        rootfs: Root filesystem filename relative to the manifest prefix.
        rootfs_sha256: Expected SHA-256 hex digest of the rootfs, or None.
        initrd: Optional initrd filename.
        initrd_sha256: Expected SHA-256 hex digest of the initrd, or None.
        boot_args: Optional override for kernel boot arguments.
    """

    name: str
    kernel: str
    kernel_sha256: str | None = None
    rootfs: str
    rootfs_sha256: str | None = None
    initrd: str | None = None
    initrd_sha256: str | None = None
    boot_args: str | None = None

    @field_validator("kernel", "rootfs", "initrd")
    @classmethod
    def validate_asset_filename(cls, value: str | None) -> str | None:
        """Reject path traversal and ensure filenames stay in the cache dir."""
        if value is None:
            return None
        path = Path(value)
        if path.is_absolute():
            raise ValueError(f"manifest asset path must be relative, got: {value!r}")
        if ".." in path.parts:
            raise ValueError(f"manifest asset path must not contain '..': {value!r}")
        # Normalize to basename (same as ImageSource)
        basename = path.name
        if not basename or basename in {".", ".."}:
            raise ValueError(f"manifest asset path must resolve to a filename: {value!r}")
        return basename

    @model_validator(mode="after")
    def _check_unique_filenames(self) -> S3ImageManifest:
        """Reject manifests where multiple assets map to the same filename."""
        filenames: dict[str, str] = {}
        for label in ("kernel", "rootfs", "initrd"):
            fname = getattr(self, label)
            if fname is None:
                continue
            existing = filenames.get(fname)
            if existing is not None:
                raise ValueError(
                    f"manifest asset filenames collide: {existing} and "
                    f"{label} both map to '{fname}'"
                )
            filenames[fname] = label
        return self

    model_config = {"frozen": True}


def parse_s3_image_uri(uri: str) -> S3ImageRef:
    """Parse an ``s3://bucket/prefix`` URI into an :class:`S3ImageRef`.

    Args:
        uri: S3 URI string (e.g. ``s3://my-bucket/images/alpine/``).

    Returns:
        Parsed :class:`S3ImageRef`.

    Raises:
        ImageError: If the URI is not a valid ``s3://`` reference.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ImageError(f"Expected an s3:// URI, got scheme {parsed.scheme!r}: {uri}")
    bucket = parsed.netloc
    if not bucket:
        raise ImageError(f"S3 URI is missing a bucket name: {uri}")
    prefix = parsed.path.strip("/")
    if not prefix:
        raise ImageError(f"S3 URI is missing an image prefix/path: {uri}")
    return S3ImageRef(bucket=bucket, prefix=prefix)


# ---------------------------------------------------------------------------
# S3 credential resolution
#
# SmolVM checks its own env vars first, then falls back to boto3's
# standard credential chain (AWS_* env vars, ~/.aws/credentials, IAM
# roles).  This lets users point SmolVM at S3-compatible stores
# (Cloudflare R2, MinIO, etc.) without touching their AWS config.
#
#   SMOLVM_S3_ENDPOINT_URL       — Custom S3 endpoint
#   SMOLVM_S3_ACCESS_KEY_ID      — Access key (falls back to AWS_ACCESS_KEY_ID)
#   SMOLVM_S3_SECRET_ACCESS_KEY  — Secret key (falls back to AWS_SECRET_ACCESS_KEY)
# ---------------------------------------------------------------------------

_S3_ENV_VARS = {
    "endpoint_url": "SMOLVM_S3_ENDPOINT_URL",
    "access_key": "SMOLVM_S3_ACCESS_KEY_ID",
    "secret_key": "SMOLVM_S3_SECRET_ACCESS_KEY",
}


def _require_boto3() -> S3Client:
    """Import boto3 and return an S3 client, or raise a helpful error.

    Credentials are resolved in order:

    1. ``SMOLVM_S3_*`` environment variables (explicit SmolVM config)
    2. ``AWS_*`` environment variables (standard boto3 chain)
    3. ``~/.aws/credentials`` / IAM roles (standard boto3 chain)

    For S3-compatible stores set at minimum::

        export SMOLVM_S3_ENDPOINT_URL=https://<id>.r2.cloudflarestorage.com
        export SMOLVM_S3_ACCESS_KEY_ID=<key>
        export SMOLVM_S3_SECRET_ACCESS_KEY=<secret>
    """
    import os

    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError:
        raise ImageError(
            "S3 image support requires boto3. Install it with:\n"
            "  pip install 'smolvm[s3]'"
        ) from None

    # Load .env file if present (walks up from cwd to find it).
    # Only sets vars not already in os.environ, so explicit exports win.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        logger.debug("python-dotenv not installed; skipping .env loading")

    kwargs: dict[str, str] = {}

    endpoint_url = os.environ.get(_S3_ENV_VARS["endpoint_url"])
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
        # S3-compatible stores (R2, MinIO) typically need region="auto"
        # to avoid boto3 sending the default AWS region which they reject.
        kwargs["region_name"] = "auto"

    access_key = os.environ.get(_S3_ENV_VARS["access_key"])
    secret_key = os.environ.get(_S3_ENV_VARS["secret_key"])
    if access_key or secret_key:
        if not (access_key and secret_key):
            missing = "SMOLVM_S3_SECRET_ACCESS_KEY" if access_key else "SMOLVM_S3_ACCESS_KEY_ID"
            raise ImageError(
                f"Incomplete S3 credentials: {missing} is not set. "
                f"Both SMOLVM_S3_ACCESS_KEY_ID and SMOLVM_S3_SECRET_ACCESS_KEY "
                f"must be set together."
            )
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    if kwargs:
        logger.info(
            "Using SmolVM S3 config: endpoint=%s, credentials=%s",
            endpoint_url or "(default)",
            "SMOLVM_S3_*" if access_key else "(boto3 chain)",
        )

    return boto3.client("s3", **kwargs)  # type: ignore[no-any-return]


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
        source = self.registry.get(name)
        if source is None:
            return False

        kernel, initrd, rootfs = self._resolve_asset_paths(image_dir, source)

        return kernel.is_file() and rootfs.is_file() and (initrd is None or initrd.is_file())

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
        kernel_path, initrd_path, rootfs_path = self._resolve_asset_paths(image_dir, source)

        # Check cache — re-download if SHA mismatch
        initrd_ready = initrd_path is None or initrd_path.is_file()
        if kernel_path.is_file() and rootfs_path.is_file() and initrd_ready:
            kernel_ok = self._verify_sha256(kernel_path, source.kernel_sha256)
            initrd_ok = (
                True
                if initrd_path is None
                else self._verify_sha256(initrd_path, source.initrd_sha256)
            )
            rootfs_ok = self._verify_sha256(rootfs_path, source.rootfs_sha256)
            if kernel_ok and initrd_ok and rootfs_ok:
                logger.info("Image '%s' found in cache: %s", name, image_dir)
                return LocalImage(
                    name=name,
                    kernel_path=kernel_path,
                    initrd_path=initrd_path,
                    rootfs_path=rootfs_path,
                )
            logger.warning("Cached image '%s' failed SHA-256 check, re-downloading", name)

        # Download
        image_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading image '%s' kernel...", name)
        self._download_file(source.kernel_url, kernel_path, source.kernel_sha256)

        if initrd_path is not None and source.initrd_url is not None:
            logger.info("Downloading image '%s' initrd...", name)
            self._download_file(source.initrd_url, initrd_path, source.initrd_sha256)

        logger.info("Downloading image '%s' rootfs...", name)
        self._download_file(source.rootfs_url, rootfs_path, source.rootfs_sha256)

        logger.info("Image '%s' ready at: %s", name, image_dir)
        return LocalImage(
            name=name,
            kernel_path=kernel_path,
            initrd_path=initrd_path,
            rootfs_path=rootfs_path,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_asset_paths(
        self,
        image_dir: Path,
        source: ImageSource,
    ) -> tuple[Path, Path | None, Path]:
        """Return validated cache destinations for image assets."""
        filenames = {
            "kernel": source.kernel_filename,
            "rootfs": source.rootfs_filename,
        }
        if source.initrd_url is not None:
            filenames["initrd"] = source.initrd_filename

        labels_by_filename: dict[str, str] = {}
        for label, filename in filenames.items():
            existing_label = labels_by_filename.get(filename)
            if existing_label is not None:
                raise ImageError(
                    "Image asset filenames collide within the cache directory: "
                    f"{existing_label} and {label} both map to '{filename}'"
                )
            labels_by_filename[filename] = label

        resolved_paths: dict[str, Path] = {}
        for label, filename in filenames.items():
            destination = image_dir / filename
            if destination.name != filename or destination.parent != image_dir:
                raise ImageError(
                    f"Image asset '{label}' must stay within the cache directory: {filename!r}"
                )
            resolved_paths[label] = destination

        return (
            resolved_paths["kernel"],
            resolved_paths.get("initrd"),
            resolved_paths["rootfs"],
        )

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

    # ------------------------------------------------------------------
    # S3 image support
    # ------------------------------------------------------------------

    def ensure_s3_image(self, uri: str) -> tuple[LocalImage, S3ImageManifest]:
        """Download and cache an S3-hosted image.

        The S3 prefix must contain a ``smolvm-image.json`` manifest that
        declares the kernel, rootfs, and optional initrd filenames along
        with their SHA-256 hashes.

        Args:
            uri: S3 URI pointing to the image prefix
                (e.g. ``s3://bucket/images/alpine-ssh/``).

        Returns:
            A tuple of the cached :class:`LocalImage` and the parsed
            :class:`S3ImageManifest`.

        Raises:
            ImageError: On invalid URI, missing manifest, download
                failure, or checksum mismatch.
        """
        ref = parse_s3_image_uri(uri)
        s3 = _require_boto3()

        # Compute a stable cache directory from the URI
        cache_key = hashlib.sha256(uri.encode()).hexdigest()[:16]
        image_dir = self.cache_dir / "s3" / cache_key

        manifest = self._fetch_s3_manifest(s3, ref, image_dir)

        kernel_path = image_dir / manifest.kernel
        rootfs_path = image_dir / manifest.rootfs
        initrd_path = image_dir / manifest.initrd if manifest.initrd else None

        # Check local cache
        kernel_ok = (
            kernel_path.is_file()
            and self._verify_sha256(kernel_path, manifest.kernel_sha256)
        )
        rootfs_ok = (
            rootfs_path.is_file()
            and self._verify_sha256(rootfs_path, manifest.rootfs_sha256)
        )
        initrd_ok = (
            initrd_path is None
            or (initrd_path.is_file() and self._verify_sha256(initrd_path, manifest.initrd_sha256))
        )

        if kernel_ok and rootfs_ok and initrd_ok:
            logger.info("S3 image '%s' found in cache: %s", manifest.name, image_dir)
        else:
            if not kernel_ok:
                logger.info("Downloading S3 image '%s' kernel...", manifest.name)
                s3_key = f"{ref.prefix}/{manifest.kernel}"
                self._download_s3_file(s3, ref.bucket, s3_key, kernel_path, manifest.kernel_sha256)

            if not rootfs_ok:
                logger.info("Downloading S3 image '%s' rootfs...", manifest.name)
                s3_key = f"{ref.prefix}/{manifest.rootfs}"
                self._download_s3_file(s3, ref.bucket, s3_key, rootfs_path, manifest.rootfs_sha256)

            if not initrd_ok and initrd_path is not None and manifest.initrd is not None:
                logger.info("Downloading S3 image '%s' initrd...", manifest.name)
                s3_key = f"{ref.prefix}/{manifest.initrd}"
                self._download_s3_file(s3, ref.bucket, s3_key, initrd_path, manifest.initrd_sha256)

            logger.info("S3 image '%s' ready at: %s", manifest.name, image_dir)

        local = LocalImage(
            name=manifest.name,
            kernel_path=kernel_path,
            initrd_path=initrd_path,
            rootfs_path=rootfs_path,
        )
        return local, manifest

    def _fetch_s3_manifest(
        self,
        s3: S3Client,
        ref: S3ImageRef,
        image_dir: Path,
    ) -> S3ImageManifest:
        """Download and parse the ``smolvm-image.json`` manifest.

        Uses an atomic temp-file-then-rename write to avoid partial
        reads by concurrent callers.  If S3 is unreachable but a
        previously cached manifest exists locally, falls back to the
        cached copy so that fully-cached images work offline.
        """
        manifest_key = f"{ref.prefix}/smolvm-image.json"
        manifest_dest = image_dir / "smolvm-image.json"
        image_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Atomic download: temp file → rename.
            # Uses get_object (single GetObject call) instead of
            # download_file/download_fileobj which issue HeadObject
            # first — some S3-compatible stores (e.g. R2) reject
            # HeadObject on certain token types.
            tmp_fd, tmp_path_str = tempfile.mkstemp(
                dir=image_dir, suffix=".tmp"
            )
            tmp_path = Path(tmp_path_str)
            try:
                with open(tmp_fd, "wb") as f:
                    response = s3.get_object(Bucket=ref.bucket, Key=manifest_key)
                    for chunk in response["Body"].iter_chunks():
                        f.write(chunk)
                tmp_path.rename(manifest_dest)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            if manifest_dest.is_file():
                logger.warning(
                    "S3 manifest refresh failed (%s); using cached copy",
                    exc,
                )
            else:
                raise ImageError(
                    f"Failed to download image manifest from "
                    f"s3://{ref.bucket}/{manifest_key}: {exc}"
                ) from exc

        try:
            raw = json.loads(manifest_dest.read_text())
            return S3ImageManifest(**raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ImageError(
                f"Invalid smolvm-image.json in s3://{ref.bucket}/{ref.prefix}/: {exc}"
            ) from exc

    def _download_s3_file(
        self,
        s3: S3Client,
        bucket: str,
        key: str,
        dest: Path,
        expected_sha256: str | None = None,
    ) -> None:
        """Download a file from S3 with atomic write and SHA-256 verification.

        Uses the same temp-file-then-rename pattern as :meth:`_download_file`.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        tmp_path = Path(tmp_path_str)

        try:
            sha256 = hashlib.sha256() if expected_sha256 else None
            # Use get_object (single GetObject call) instead of
            # download_fileobj which issues HeadObject first — some
            # S3-compatible stores reject HeadObject.
            with open(tmp_fd, "wb") as f:
                response = s3.get_object(Bucket=bucket, Key=key)
                for chunk in response["Body"].iter_chunks(_DOWNLOAD_CHUNK_SIZE):
                    f.write(chunk)
                    if sha256 is not None:
                        sha256.update(chunk)

            if sha256 is not None and expected_sha256 is not None:
                actual_hash = sha256.hexdigest()
                if actual_hash != expected_sha256:
                    raise ImageError(
                        f"SHA-256 mismatch for s3://{bucket}/{key}\n"
                        f"  expected: {expected_sha256}\n"
                        f"  actual:   {actual_hash}"
                    )

            tmp_path.rename(dest)
            logger.debug("Downloaded s3://%s/%s -> %s", bucket, key, dest)

        except ImageError:
            raise
        except Exception as exc:
            raise ImageError(
                f"S3 download failed for s3://{bucket}/{key}: {exc}"
            ) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
