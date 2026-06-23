"""Tests for the public smolvm_core Python shim."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
import smolvm_core


def test_capability_helpers_are_explicit_and_compatible() -> None:
    """The module should separate networking and QMP availability."""
    assert "has_native_networking" in smolvm_core.__all__
    assert "has_native_qmp" in smolvm_core.__all__
    assert "has_native_disk_io" in smolvm_core.__all__
    assert "has_native_firecracker_api" in smolvm_core.__all__
    assert isinstance(smolvm_core.has_native_networking(), bool)
    assert isinstance(smolvm_core.has_native_qmp(), bool)
    assert isinstance(smolvm_core.has_native_disk_io(), bool)
    assert isinstance(smolvm_core.has_native_firecracker_api(), bool)
    assert smolvm_core.is_available() is smolvm_core.has_native_networking()


def test_public_network_functions_have_python_signatures() -> None:
    """Wrappers should expose useful names to pydoc, inspect, and editors."""
    signature = inspect.signature(smolvm_core.create_tap)

    assert list(signature.parameters) == ["name", "owner_uid"]
    assert signature.parameters["name"].annotation is str
    assert signature.parameters["owner_uid"].annotation is int
    assert signature.return_annotation is None
    assert "Create a TAP network device" in inspect.getdoc(smolvm_core.create_tap)

    configure_signature = inspect.signature(smolvm_core.configure_tap)
    assert list(configure_signature.parameters) == ["name", "host_ip", "prefix_len"]
    assert configure_signature.parameters["name"].annotation is str
    assert configure_signature.parameters["host_ip"].annotation is str
    assert configure_signature.parameters["prefix_len"].annotation is int
    assert configure_signature.return_annotation is None
    assert "Assign an IPv4 address" in inspect.getdoc(smolvm_core.configure_tap)

    prepare_signature = inspect.signature(smolvm_core.prepare_tap)
    assert list(prepare_signature.parameters) == [
        "name",
        "owner_uid",
        "host_ip",
        "prefix_len",
        "route_localnet",
    ]
    assert prepare_signature.parameters["route_localnet"].default is True
    assert prepare_signature.return_annotation is None
    assert "Create and configure a TAP link" in inspect.getdoc(smolvm_core.prepare_tap)


def test_qmp_native_class_stays_private() -> None:
    """QMP users should go through smolvm.qmp.QMPClient, not smolvm_core."""
    assert "_QmpClient" not in smolvm_core.__all__
    assert not hasattr(smolvm_core, "_QmpClient")
    assert "QMPClient" in inspect.getdoc(smolvm_core.has_native_qmp)


def test_firecracker_native_helpers_stay_private() -> None:
    """Firecracker users should go through smolvm.api.FirecrackerClient."""
    assert "_firecracker_request" not in smolvm_core.__all__
    assert "_firecracker_wait_for_socket" not in smolvm_core.__all__
    assert "Firecracker API accelerator" in inspect.getdoc(smolvm_core.has_native_firecracker_api)


def test_public_disk_functions_have_python_signatures() -> None:
    """Disk helpers should expose stable wrapper signatures to Python callers."""
    copy_signature = inspect.signature(smolvm_core.clone_or_sparse_copy)
    assert list(copy_signature.parameters) == ["source", "target"]
    assert copy_signature.parameters["source"].annotation is str
    assert copy_signature.parameters["target"].annotation is str
    assert copy_signature.return_annotation is str

    decompress_signature = inspect.signature(smolvm_core.decompress_zstd_sparse)
    assert list(decompress_signature.parameters) == ["source", "target", "chunk_size"]
    assert decompress_signature.parameters["chunk_size"].default == 1048576
    assert decompress_signature.return_annotation is str


def _write_sparse_file(path: Path) -> None:
    with path.open("wb") as file:
        file.write(b"start")
        file.seek(4 * 1024 * 1024)
        file.write(b"end")


def _assert_sparse_image(path: Path, expected: bytes) -> None:
    assert path.read_bytes() == expected
    assert path.stat().st_blocks * 512 < path.stat().st_size


def test_native_clone_or_sparse_copy_preserves_sparse_holes(tmp_path: Path) -> None:
    """Native disk copy should keep sparse raw-rootfs holes sparse."""
    source = tmp_path / "source.ext4"
    target = tmp_path / "target.ext4"
    _write_sparse_file(source)

    method = smolvm_core.clone_or_sparse_copy(str(source), str(target))

    assert method in {"reflink", "sparse"}
    assert target.stat().st_size == source.stat().st_size
    with target.open("rb") as file:
        assert file.read(5) == b"start"
        file.seek(4 * 1024 * 1024)
        assert file.read(3) == b"end"
    assert target.stat().st_blocks * 512 < target.stat().st_size


def test_native_decompress_zstd_sparse_preserves_sparse_holes(tmp_path: Path) -> None:
    """Native zstd decompression should avoid allocating zero-filled ranges."""
    zstandard = pytest.importorskip("zstandard")
    plain = b"start" + (b"\0" * (4 * 1024 * 1024)) + b"end"
    source = tmp_path / "rootfs.ext4.zst"
    target = tmp_path / "rootfs.ext4"
    source.write_bytes(zstandard.ZstdCompressor(level=3).compress(plain))

    method = smolvm_core.decompress_zstd_sparse(str(source), str(target), 1024 * 1024)

    assert method == "sparse"
    _assert_sparse_image(target, plain)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific unsupported helper path")
def test_write_sysctl_reports_unsupported_platform_on_macos() -> None:
    """macOS should get the same unsupported-platform error as TAP helpers."""
    with pytest.raises(OSError, match="Not available on this platform"):
        smolvm_core.write_sysctl("net.ipv4.ip_forward", "1")
