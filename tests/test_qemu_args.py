# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Byte-identical-output tests for the pure QEMU argv builder.

The builder ``build_qemu_argv`` was extracted from
``SmolVMManager._start_qemu`` so it can be unit-tested without spawning
QEMU. For the Linux all-defaults platform spec (``_LINUX_SPEC``) it must
produce output byte-for-byte equivalent to the pre-refactor code path.
The tests in this file lock that invariant: any change to the Linux argv
shape must be intentional and explicitly captured here.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime.guest_platforms import (
    _LINUX_SPEC,
    FirmwareSpec,
    _build_windows_spec,
)
from smolvm.runtime.qemu_args import build_qemu_argv
from smolvm.types import GuestOS, NetworkConfig, VMConfig, VMInfo, VMState


def _qemu_vm_info(tmp_path: Path, *, vm_id: str = "vm-test") -> VMInfo:
    """A minimal Linux VMInfo wired for the QEMU backend."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    return VMInfo(
        vm_id=vm_id,
        status=VMState.CREATED,
        config=VMConfig(
            vm_id=vm_id,
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            boot_args="console=ttyS0 reboot=k panic=1 init=/init",
        ),
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:12:34:56",
            ssh_host_port=2200,
        ),
    )


def test_linux_x86_64_kvm_argv_byte_identical(tmp_path: Path) -> None:
    """Linux x86_64 + KVM produces the legacy q35 argv exactly."""
    vm_info = _qemu_vm_info(tmp_path)
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )

    assert cmd == [
        "/usr/bin/qemu-system-x86_64",
        "-smp",
        "2",
        "-m",
        "512",
        "-kernel",
        str(vm_info.config.kernel_path),
        "-append",
        "console=ttyS0 reboot=k panic=1 init=/init",
        "-drive",
        (
            f"file={vm_info.config.rootfs_path},if=none,format=raw,"
            "id=rootdisk0-drive,node-name=rootdisk0"
        ),
        "-netdev",
        "user,id=net0,dns=10.0.2.3,hostfwd=tcp:127.0.0.1:2200-:22",
        "-nographic",
        "-no-reboot",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-device",
        "virtio-blk-pci,drive=rootdisk0-drive",
        "-device",
        "virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56",
    ]


def test_linux_x86_64_darwin_uses_hvf(tmp_path: Path) -> None:
    """Darwin host swaps accel=kvm for accel=hvf; nothing else changes."""
    vm_info = _qemu_vm_info(tmp_path)
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/opt/homebrew/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Darwin",
    )

    assert "-machine" in cmd
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg == "q35,accel=hvf"


def test_linux_aarch64_kvm_orders_rootdisk_last(tmp_path: Path) -> None:
    """aarch64 virt boots emit virtio-blk-device for root LAST (virtio-MMIO
    reverse enumeration). Lock the exact ordering invariant."""
    vm_info = _qemu_vm_info(tmp_path)
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/opt/homebrew/bin/qemu-system-aarch64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )

    # Both -device pairs in order: NIC then root disk (root disk must be last).
    device_pairs = [
        (cmd[i + 1])
        for i, tok in enumerate(cmd)
        if tok == "-device"
    ]
    assert device_pairs == [
        "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56",
        "virtio-blk-device,drive=rootdisk0-drive",
    ]
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg == "virt,accel=kvm"


def test_firmware_mode_aarch64_needs_uefi_firmware(tmp_path: Path) -> None:
    """aarch64 firmware-boot raises a clear error when OVMF is absent."""
    rootfs = tmp_path / "ubuntu.qcow2"
    rootfs.touch()
    config = VMConfig(
        vm_id="vm-ubuntu",
        kernel_path=None,
        rootfs_path=rootfs,
        backend="qemu",
        boot_mode="firmware",
        boot_args="",
    )
    vm_info = VMInfo(
        vm_id="vm-ubuntu",
        status=VMState.CREATED,
        config=config,
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:12:34:56",
            ssh_host_port=2201,
        ),
    )

    with patch(
        "smolvm.runtime.qemu_args._find_aarch64_uefi_firmware",
        return_value=None,
    ), pytest.raises(SmolVMError, match="aarch64 firmware-boot requires UEFI firmware"):
        build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu-system-aarch64"),
            boot_args="",
            platform_spec=_LINUX_SPEC,
            host_system="Linux",
        )


def test_missing_ssh_host_port_raises(tmp_path: Path) -> None:
    """The QEMU backend requires a reserved ssh_host_port."""
    vm_info = _qemu_vm_info(tmp_path)
    # Pydantic frozen model — swap the network for one without the port.
    vm_info = vm_info.model_copy(
        update={
            "network": vm_info.network.model_copy(update={"ssh_host_port": None})
        }
    )
    with pytest.raises(SmolVMError, match="ssh_host_port"):
        build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
            boot_args=vm_info.config.boot_args,
            platform_spec=_LINUX_SPEC,
            host_system="Linux",
        )
