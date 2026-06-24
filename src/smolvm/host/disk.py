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

"""Host disk helpers for sparse VM images.

SmolVM stores raw rootfs images with large zeroed regions. These helpers keep
those regions sparse when cloning or decompressing images so a sandbox does not
spend time writing unused disk blocks before boot.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

_NATIVE_DISABLE_ENV = "SMOLVM_DISABLE_NATIVE_DISK"
_TRUE_ENV_VALUES = {"1", "true", "yes"}
_SPARSE_CHUNK_SIZE = 1024 * 1024

try:
    from smolvm_core import disk as core_disk
except ImportError:  # pragma: no cover - depends on optional wheel availability
    core_disk = None  # type: ignore[assignment]


def has_native_disk_io() -> bool:
    """Return True when Rust disk/image helpers are enabled for this process."""

    return _native_available()


def clone_or_sparse_copy(source_path: Path | str, target_path: Path | str) -> str:
    """Copy a disk image with reflink/sparse I/O when available.

    Returns a short method label for logging/tests. Callers that only need the
    copy side effect can ignore the return value.
    """

    source = Path(source_path)
    target = Path(target_path)
    _ensure_parent_dir(target)

    if method := _cp_clone_or_sparse_copy(source, target):
        return method

    if _native_available():
        return _native_clone_or_sparse_copy(source, target)

    return _python_clone_or_sparse_copy(source, target)


def decompress_zstd_sparse(
    source_path: Path | str,
    target_path: Path | str,
    *,
    chunk_size: int = _SPARSE_CHUNK_SIZE,
) -> str:
    """Decompress a zstd image while preserving zero ranges as sparse holes."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    source = Path(source_path)
    target = Path(target_path)
    _ensure_parent_dir(target)

    if _native_available():
        method = str(core_disk.decompress_zstd_sparse(str(source), str(target), chunk_size))
        return f"native:{method}"

    return _python_decompress_zstd_sparse(source, target, chunk_size=chunk_size)


def _native_available() -> bool:
    disabled = os.environ.get(_NATIVE_DISABLE_ENV, "").strip().lower() in _TRUE_ENV_VALUES
    if disabled or core_disk is None:
        return False
    try:
        return bool(core_disk.available())
    except OSError:
        return False


def _cp_clone_or_sparse_copy(source: Path, target: Path) -> str | None:
    try:
        result = subprocess.run(
            ["cp", "--reflink=auto", "--sparse=always", str(source), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        logger.debug("cp --reflink=auto unavailable; trying next sparse copy method: %s", exc)
    else:
        if result.returncode == 0:
            shutil.copystat(source, target)
            return "cp"
        logger.debug(
            "cp --reflink=auto failed for %s -> %s; trying next sparse copy method: %s",
            source,
            target,
            (result.stderr or "").strip(),
        )
    return None


def _native_clone_or_sparse_copy(source: Path, target: Path) -> str:
    method = str(core_disk.clone_or_sparse_copy(str(source), str(target)))
    shutil.copystat(source, target)
    return f"native:{method}"


def _python_clone_or_sparse_copy(source: Path, target: Path) -> str:
    _copy_sparse_preserving(source, target)
    return "sparse"


def _copy_sparse_preserving(
    source: Path,
    target: Path,
    *,
    chunk_size: int = _SPARSE_CHUNK_SIZE,
) -> None:
    with source.open("rb") as src, target.open("wb") as dst:
        while chunk := src.read(chunk_size):
            _write_sparse_chunk(dst, chunk)
        dst.truncate(source.stat().st_size)
    shutil.copystat(source, target)


def _python_decompress_zstd_sparse(source: Path, target: Path, *, chunk_size: int) -> str:
    import zstandard

    tmp = target.parent / (target.name + ".tmp")
    try:
        with source.open("rb") as src_f, tmp.open("wb") as target_f:
            reader = zstandard.ZstdDecompressor().stream_reader(src_f)
            size = 0
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                _write_sparse_chunk(target_f, chunk)
            target_f.truncate(size)
        tmp.replace(target)
        return "python"
    finally:
        tmp.unlink(missing_ok=True)


def _write_sparse_chunk(target: BinaryIO, chunk: bytes) -> None:
    if chunk.strip(b"\0"):
        target.write(chunk)
    else:
        target.seek(len(chunk), os.SEEK_CUR)


def _ensure_parent_dir(path: Path) -> None:
    parent = path.parent
    if parent != Path(""):
        parent.mkdir(parents=True, exist_ok=True)


__all__ = [
    "has_native_disk_io",
    "clone_or_sparse_copy",
    "decompress_zstd_sparse",
]
