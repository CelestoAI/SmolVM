"""Tests for the public smolvm_core Python package."""

from __future__ import annotations

import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
import smolvm_core


def test_public_package_exports_library_modules() -> None:
    """The top level should guide users to modules, not private native symbols."""
    assert smolvm_core.__all__ == [
        "CoreCapabilities",
        "capabilities",
        "detect",
        "disk",
        "errors",
        "firecracker",
        "network",
        "qmp",
    ]
    assert hasattr(smolvm_core, "network")
    assert hasattr(smolvm_core, "disk")
    assert hasattr(smolvm_core, "qmp")
    assert hasattr(smolvm_core, "firecracker")
    assert not hasattr(smolvm_core, "create_tap")
    assert not hasattr(smolvm_core, "has_native_networking")
    assert not hasattr(smolvm_core, "_QmpClient")
    assert "_ffi" not in smolvm_core.__all__


def test_old_extension_module_name_is_removed() -> None:
    """The alpha break should not leave the old private module importable."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("smolvm_core._smolvm_core")


def test_capability_report_is_structured() -> None:
    """Capability checks should be discoverable and JSON-friendly."""
    caps = smolvm_core.detect()

    assert isinstance(caps, smolvm_core.CoreCapabilities)
    assert isinstance(caps.networking, bool)
    assert isinstance(caps.disk_io, bool)
    assert isinstance(caps.qmp, bool)
    assert isinstance(caps.firecracker_api, bool)
    assert caps.as_dict() == {
        "networking": caps.networking,
        "disk_io": caps.disk_io,
        "qmp": caps.qmp,
        "firecracker_api": caps.firecracker_api,
    }


def test_python_module_entrypoint_reports_capabilities() -> None:
    """`python -m smolvm_core` should give contributors a quick health check."""
    result = subprocess.run(
        [sys.executable, "-m", "smolvm_core"],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)

    assert "smolvm_core" in report
    assert isinstance(report["capabilities"]["networking"], bool)
    assert isinstance(report["capabilities"]["disk_io"], bool)
    assert isinstance(report["capabilities"]["qmp"], bool)
    assert isinstance(report["capabilities"]["firecracker_api"], bool)


def test_public_network_functions_have_python_signatures() -> None:
    """Wrappers should expose useful names to pydoc, inspect, and editors."""
    signature = inspect.signature(smolvm_core.network.create_tap)

    assert list(signature.parameters) == ["name", "owner_uid"]
    assert signature.parameters["name"].annotation is str
    assert signature.parameters["owner_uid"].annotation is int
    assert signature.return_annotation is None
    assert "Create a TAP network device" in inspect.getdoc(smolvm_core.network.create_tap)

    configure_signature = inspect.signature(smolvm_core.network.configure_tap)
    assert list(configure_signature.parameters) == ["name", "host_ip", "prefix_len"]
    assert configure_signature.parameters["name"].annotation is str
    assert configure_signature.parameters["host_ip"].annotation is str
    assert configure_signature.parameters["prefix_len"].annotation is int
    assert configure_signature.return_annotation is None
    assert "Assign an IPv4 address" in inspect.getdoc(smolvm_core.network.configure_tap)

    prepare_signature = inspect.signature(smolvm_core.network.prepare_tap)
    assert list(prepare_signature.parameters) == [
        "name",
        "owner_uid",
        "host_ip",
        "prefix_len",
        "route_localnet",
    ]
    assert prepare_signature.parameters["route_localnet"].default is True
    assert prepare_signature.return_annotation is None
    assert "Create and configure a TAP link" in inspect.getdoc(smolvm_core.network.prepare_tap)


def test_public_disk_functions_have_python_signatures() -> None:
    """Disk helpers should expose stable wrapper signatures to Python callers."""
    copy_signature = inspect.signature(smolvm_core.disk.clone_or_sparse_copy)
    assert list(copy_signature.parameters) == ["source", "target"]
    assert copy_signature.parameters["source"].annotation is str
    assert copy_signature.parameters["target"].annotation is str
    assert copy_signature.return_annotation is str

    decompress_signature = inspect.signature(smolvm_core.disk.decompress_zstd_sparse)
    assert list(decompress_signature.parameters) == ["source", "target", "chunk_size"]
    assert decompress_signature.parameters["chunk_size"].default == 1048576
    assert decompress_signature.return_annotation is str


def test_qmp_and_firecracker_are_public_modules() -> None:
    """Control-plane users should import real modules, not private extension names."""
    assert "QMPClient" in smolvm_core.qmp.__all__
    assert "QMPJob" in smolvm_core.qmp.__all__
    assert "FirecrackerClient" in smolvm_core.firecracker.__all__
    assert inspect.isclass(smolvm_core.qmp.QMPClient)
    assert inspect.isclass(smolvm_core.firecracker.FirecrackerClient)


def test_firecracker_request_wraps_socket_errors(tmp_path: Path) -> None:
    """Standalone Firecracker requests should raise the public core error type."""
    if not smolvm_core.firecracker.available():
        pytest.skip("native Firecracker API support is unavailable")

    client = smolvm_core.firecracker.FirecrackerClient(tmp_path / "missing.sock")

    with pytest.raises(
        smolvm_core.errors.FirecrackerAPIError,
        match="Could not reach Firecracker API socket",
    ):
        client.request("GET", "/")


def test_production_code_does_not_import_private_core_extension() -> None:
    """SmolVM production code should use public smolvm_core modules only."""
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "src" / "smolvm"
    forbidden = [
        "smolvm_core._ffi",
        "smolvm_core._smolvm_core",
        "import smolvm_core as _native",
        "import smolvm_core as native",
        "_firecracker_request",
        "_firecracker_wait_for_socket",
        "_QmpClient",
    ]
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        text = path.read_text()
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(repo_root)} contains {needle}")

    assert offenders == []


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

    method = smolvm_core.disk.clone_or_sparse_copy(str(source), str(target))

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

    method = smolvm_core.disk.decompress_zstd_sparse(str(source), str(target), 1024 * 1024)

    assert method == "sparse"
    _assert_sparse_image(target, plain)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific unsupported helper path")
def test_write_sysctl_reports_unsupported_platform_on_macos() -> None:
    """macOS should get the same unsupported-platform error as TAP helpers."""
    with pytest.raises(OSError, match="Not available on this platform"):
        smolvm_core.network.write_sysctl("net.ipv4.ip_forward", "1")
