"""Tests for the public smolvm_core Python shim."""

from __future__ import annotations

import inspect
import sys

import pytest
import smolvm_core


def test_capability_helpers_are_explicit_and_compatible() -> None:
    """The module should separate networking and QMP availability."""
    assert "has_native_networking" in smolvm_core.__all__
    assert "has_native_qmp" in smolvm_core.__all__
    assert isinstance(smolvm_core.has_native_networking(), bool)
    assert isinstance(smolvm_core.has_native_qmp(), bool)
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


def test_qmp_native_class_stays_private() -> None:
    """QMP users should go through smolvm.qmp.QMPClient, not smolvm_core."""
    assert "_QmpClient" not in smolvm_core.__all__
    assert not hasattr(smolvm_core, "_QmpClient")
    assert "QMPClient" in inspect.getdoc(smolvm_core.has_native_qmp)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific unsupported helper path")
def test_write_sysctl_reports_unsupported_platform_on_macos() -> None:
    """macOS should get the same unsupported-platform error as TAP helpers."""
    with pytest.raises(OSError, match="Not available on this platform"):
        smolvm_core.write_sysctl("net.ipv4.ip_forward", "1")
