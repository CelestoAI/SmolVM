"""Sparse disk-image helpers."""

from . import _ffi


def available() -> bool:
    """Return True when this wheel includes native disk/image helpers."""

    return bool(_ffi.has_native_disk_io())


def clone_or_sparse_copy(source: str, target: str) -> str:
    """Copy a disk image using reflink or sparse-preserving native I/O."""

    return str(_ffi.clone_or_sparse_copy(source, target))


def decompress_zstd_sparse(source: str, target: str, chunk_size: int = 1048576) -> str:
    """Decompress a zstd image while preserving zero regions as sparse holes."""

    return str(_ffi.decompress_zstd_sparse(source, target, chunk_size))


__all__ = [
    "available",
    "clone_or_sparse_copy",
    "decompress_zstd_sparse",
]
