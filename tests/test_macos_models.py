# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path

import pytest
from pydantic import ValidationError

from smolvm.macos.models import MacOSInstallRequest, MacOSRunRequest
from smolvm.types import (
    DesktopEndpoint,
    GuestOS,
    InternetSettings,
    MacOSMachineConfig,
    NetworkAttachmentConfig,
    VMConfig,
)


def _machine(tmp_path: Path) -> MacOSMachineConfig:
    return MacOSMachineConfig(
        base_image="macos-latest",
        manifest_path=tmp_path / "manifest.json",
        bundle_path=tmp_path / "vm.bundle",
        guest_version="26.0",
    )


def test_lume_requests_reject_option_like_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot begin with"):
        MacOSInstallRequest(name="--help", storage_path=tmp_path)
    with pytest.raises(ValueError, match="cannot begin with"):
        MacOSRunRequest(name="--help", storage_path=tmp_path)

    assert MacOSInstallRequest(name="macos-latest", storage_path=tmp_path).name == "macos-latest"
    assert MacOSRunRequest(name="mac-test", storage_path=tmp_path).name == "mac-test"


def test_macos_vm_config_uses_platform_bundle(tmp_path: Path) -> None:
    config = VMConfig(
        vm_id="mac-test",
        guest_os=GuestOS.MACOS,
        backend="vz",
        boot_mode="platform",
        macos_machine=_machine(tmp_path),
        memory=8192,
    )

    assert config.rootfs_path is None
    assert config.macos_machine is not None
    assert config.macos_machine.ssh_user == "lume"


def test_macos_vm_requires_vz_platform_bundle(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    with pytest.raises(ValidationError, match="backend='vz'"):
        VMConfig(
            guest_os=GuestOS.MACOS,
            backend="qemu",
            boot_mode="platform",
            macos_machine=machine,
        )
    with pytest.raises(ValidationError, match="macos_machine"):
        VMConfig(guest_os=GuestOS.MACOS, backend="vz", boot_mode="platform")


def test_macos_vm_rejects_linux_artifacts_and_unsupported_controls(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    rootfs = tmp_path / "rootfs.ext4"
    rootfs.touch()

    with pytest.raises(ValidationError, match="machine bundle"):
        VMConfig(
            guest_os=GuestOS.MACOS,
            backend="vz",
            boot_mode="platform",
            macos_machine=machine,
            rootfs_path=rootfs,
        )
    with pytest.raises(ValidationError, match="do not support vsock"):
        VMConfig(
            guest_os=GuestOS.MACOS,
            backend="vz",
            boot_mode="platform",
            macos_machine=machine,
            comm_channel="vsock",
        )
    with pytest.raises(ValidationError, match="remove env_vars"):
        VMConfig(
            guest_os=GuestOS.MACOS,
            backend="vz",
            boot_mode="platform",
            macos_machine=machine,
            env_vars={"TOKEN": "secret"},
        )
    with pytest.raises(ValidationError, match="do not support domain restrictions"):
        VMConfig(
            guest_os=GuestOS.MACOS,
            backend="vz",
            boot_mode="platform",
            macos_machine=machine,
            internet_settings=InternetSettings(allowed_domains=["example.com"]),
        )
    with pytest.raises(ValidationError, match="use NAT"):
        VMConfig(
            guest_os=GuestOS.MACOS,
            backend="vz",
            boot_mode="platform",
            macos_machine=machine,
            network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br0"),
            guest_managed_networking=True,
        )


def test_non_macos_vm_rejects_vz_backend(tmp_path: Path) -> None:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    with pytest.raises(ValidationError, match="backend='vz'.*guest_os='macos'"):
        VMConfig(kernel_path=kernel, rootfs_path=rootfs, backend="vz")


def test_non_macos_vm_rejects_macos_bundle(tmp_path: Path) -> None:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    with pytest.raises(ValidationError, match="only valid for guest_os='macos'"):
        VMConfig(
            kernel_path=kernel,
            rootfs_path=rootfs,
            macos_machine=_machine(tmp_path),
        )


def test_desktop_endpoint_is_loopback_only() -> None:
    endpoint = DesktopEndpoint(port=5901, width=1440, height=900)
    assert endpoint.viewer_url == "vnc://127.0.0.1:5901"

    with pytest.raises(ValidationError, match="loopback"):
        DesktopEndpoint(host="0.0.0.0", port=5901)
