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
from smolvm.types import (
    GuestOS,
    NetworkConfig,
    VMConfig,
    VMInfo,
    VMState,
    VsockConfig,
    WorkspaceMount,
)


@pytest.fixture(autouse=True)
def _clear_experimental_qemu_machine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMOLVM_QEMU_MACHINE", raising=False)


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
        "-rtc",
        "base=utc,clock=host",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-device",
        "virtio-blk-pci,drive=rootdisk0-drive",
        "-device",
        "virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56",
    ]


def test_root_drive_format_uses_declared_rootfs_format(tmp_path: Path) -> None:
    """A raw-ext4 disk with a .qcow2 suffix is still passed to QEMU as raw."""
    vm_info = _qemu_vm_info(tmp_path)
    misleading_rootfs = tmp_path / "raw-rootfs.qcow2"
    misleading_rootfs.touch()
    config = vm_info.config.model_copy(
        update={"rootfs_path": misleading_rootfs, "rootfs_format": "raw-ext4"}
    )
    vm_info = vm_info.model_copy(update={"config": config})

    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )

    drive_arg = cmd[cmd.index("-drive") + 1]
    assert f"file={misleading_rootfs},if=none,format=raw," in drive_arg


def _with_vsock(vm_info: VMInfo, guest_cid: int = 42) -> VMInfo:
    config = vm_info.config.model_copy(update={"vsock": VsockConfig(guest_cid=guest_cid)})
    return vm_info.model_copy(update={"config": config})


def test_vsock_device_emitted_on_linux_x86(tmp_path: Path) -> None:
    vm_info = _with_vsock(_qemu_vm_info(tmp_path))
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )
    assert "-device" in cmd
    assert "vhost-vsock-pci,guest-cid=42" in cmd


def test_experimental_qemu_microvm_emits_mmio_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMOLVM_QEMU_MACHINE", "microvm")
    vm_info = _with_vsock(_qemu_vm_info(tmp_path))
    extra_drive = tmp_path / "extra.raw"
    extra_drive.touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = vm_info.config.model_copy(
        update={
            "extra_drives": [extra_drive],
            "workspace_mounts": [WorkspaceMount(host_path=workspace, mount_tag="ws0")],
        }
    )
    vm_info = vm_info.model_copy(update={"config": config})

    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )

    assert cmd[cmd.index("-machine") + 1] == (
        "microvm,accel=kvm,acpi=on,pcie=off,pic=off,pit=off,rtc=on"
    )
    assert cmd[cmd.index("-serial") + 1] == "stdio"
    assert cmd[cmd.index("-monitor") + 1] == "none"
    assert "-nodefaults" in cmd
    device_args = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-device"]
    assert "virtio-blk-device,drive=extra0-drive" in device_args
    assert "virtio-9p-device,fsdev=fsdev-ws0,mount_tag=ws0" in device_args
    assert "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56" in device_args
    assert "virtio-blk-device,drive=rootdisk0-drive" in device_args
    assert "vhost-vsock-device,guest-cid=42" in device_args
    assert not any(arg.startswith("virtio-blk-pci,") for arg in device_args)
    assert not any(arg.startswith("virtio-net-pci,") for arg in device_args)
    assert "vhost-vsock-pci,guest-cid=42" not in device_args


def test_experimental_qemu_microvm_ignored_on_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMOLVM_QEMU_MACHINE", "microvm")
    cmd = build_qemu_argv(
        _with_vsock(_qemu_vm_info(tmp_path)),
        qemu_bin=Path("/opt/homebrew/bin/qemu-system-x86_64"),
        boot_args="console=ttyS0 root=/dev/vda rw init=/init",
        platform_spec=_LINUX_SPEC,
        host_system="Darwin",
    )

    assert cmd[cmd.index("-machine") + 1] == "q35,accel=hvf"
    assert "-nodefaults" not in cmd
    assert not any("vhost-vsock" in arg for arg in cmd)


def test_experimental_qemu_microvm_ignored_for_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMOLVM_QEMU_MACHINE", "microvm")
    spec = _fake_windows_spec()
    cmd = build_qemu_argv(
        _windows_vm_info(tmp_path),
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="",
        platform_spec=spec,
        firmware_vars_path=Path("/state/OVMF_VARS.fd"),
        swtpm_socket=Path("/state/swtpm-sock"),
        host_system="Linux",
    )

    assert cmd[cmd.index("-machine") + 1].startswith("q35,accel=kvm")
    assert "-nodefaults" not in cmd


def test_vsock_device_uses_mmio_variant_on_aarch64(tmp_path: Path) -> None:
    vm_info = _with_vsock(_qemu_vm_info(tmp_path))
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-aarch64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )
    assert "vhost-vsock-device,guest-cid=42" in cmd
    assert "vhost-vsock-pci,guest-cid=42" not in cmd


def test_vsock_device_omitted_on_darwin(tmp_path: Path) -> None:
    vm_info = _with_vsock(_qemu_vm_info(tmp_path))
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Darwin",
    )
    assert not any("vhost-vsock" in arg for arg in cmd)


def test_no_vsock_device_when_config_has_none(tmp_path: Path) -> None:
    vm_info = _qemu_vm_info(tmp_path)
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )
    assert not any("vhost-vsock" in arg for arg in cmd)


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
    device_pairs = [(cmd[i + 1]) for i, tok in enumerate(cmd) if tok == "-device"]
    assert device_pairs == [
        "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56",
        "virtio-blk-device,drive=rootdisk0-drive",
    ]
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg == "virt,accel=kvm"


@pytest.mark.parametrize(
    "qemu_bin",
    [
        Path("/usr/bin/qemu-system-x86_64"),
        Path("/opt/homebrew/bin/qemu-system-aarch64"),
    ],
)
def test_linux_pins_rtc_to_host_clock(tmp_path: Path, qemu_bin: Path) -> None:
    """Linux guests get -rtc base=utc,clock=host so the guest can recover
    from host-sleep clock drift by re-reading the RTC (issue #330)."""
    vm_info = _qemu_vm_info(tmp_path)
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=qemu_bin,
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )
    assert "-rtc" in cmd
    assert cmd[cmd.index("-rtc") + 1] == "base=utc,clock=host"


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

    with (
        patch(
            "smolvm.runtime.qemu_args._find_aarch64_uefi_firmware",
            return_value=None,
        ),
        pytest.raises(SmolVMError, match="aarch64 firmware-boot requires UEFI firmware"),
    ):
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
        update={"network": vm_info.network.model_copy(update={"ssh_host_port": None})}
    )
    with pytest.raises(SmolVMError, match="ssh_host_port"):
        build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
            boot_args=vm_info.config.boot_args,
            platform_spec=_LINUX_SPEC,
            host_system="Linux",
        )


# ───────────────────────── Windows guest tests ──────────────────────────


def _windows_vm_info(tmp_path: Path, *, vm_id: str = "vm-win") -> VMInfo:
    """A minimal Windows VMInfo wired for the QEMU backend."""
    rootfs = tmp_path / "win11.qcow2"
    rootfs.touch()
    return VMInfo(
        vm_id=vm_id,
        status=VMState.CREATED,
        config=VMConfig(
            vm_id=vm_id,
            kernel_path=None,
            rootfs_path=rootfs,
            backend="qemu",
            guest_os=GuestOS.WINDOWS,
            boot_mode="firmware",
        ),
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:5d:00:01",
            ssh_host_port=2201,
        ),
    )


def _fake_windows_spec() -> object:
    """Build a Windows GuestPlatformSpec with mocked OVMF discovery."""
    fake = FirmwareSpec(
        code_path=Path("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd"),
        vars_template_path=Path("/usr/share/OVMF/OVMF_VARS_4M.ms.fd"),
    )
    with patch(
        "smolvm.runtime.guest_platforms._find_x86_64_ovmf",
        return_value=fake,
    ):
        return _build_windows_spec(host_system="Linux", arch="x86_64")


def test_build_windows_spec_raises_on_macos_host() -> None:
    """Windows guests are Linux-host-only in this release."""
    with pytest.raises(NotImplementedError, match="Linux hosts"):
        _build_windows_spec(host_system="Darwin", arch="x86_64")


def test_build_windows_spec_raises_on_arm_host() -> None:
    """Windows-on-ARM is out of Phase 1 scope."""
    with pytest.raises(NotImplementedError, match="aarch64|arm"):
        _build_windows_spec(host_system="Linux", arch="aarch64")


def test_build_windows_spec_raises_when_no_ovmf_found() -> None:
    """Missing OVMF Secure Boot firmware raises a plain-English install hint."""
    with (
        patch(
            "smolvm.runtime.guest_platforms._find_x86_64_ovmf",
            return_value=None,
        ),
        pytest.raises(ValueError, match="OVMF Secure Boot firmware"),
    ):
        _build_windows_spec(host_system="Linux", arch="x86_64")


def test_build_windows_spec_populates_expected_fields() -> None:
    """The Windows spec carries every override the QEMU builder needs."""
    spec = _fake_windows_spec()
    assert spec.guest_os is GuestOS.WINDOWS
    assert spec.name == "windows"
    assert spec.forced_boot_mode == "firmware"
    assert spec.skip_kernel_cmdline_injection is True
    assert spec.skip_workspace_mounts is True
    assert "smm=on" in spec.machine_extra_opts
    assert "vmport=off" in spec.machine_extra_opts
    assert "kernel-irqchip=on" in spec.machine_extra_opts
    assert "hv_relaxed" in spec.cpu_extra_flags
    assert "hv_vapic" in spec.cpu_extra_flags
    assert "hv_spinlocks=0x1fff" in spec.cpu_extra_flags
    assert spec.root_disk_controller == "virtio-scsi-pci"
    assert spec.root_disk_device == "scsi-hd"
    assert spec.cdrom_bus == "ide"
    assert spec.firmware is not None
    assert spec.requires_swtpm is True
    assert spec.swtpm_device_model == "tpm-crb"
    # Trailing -device entries: USB controller, then tablet binding to it.
    assert spec.extra_devices == (
        "qemu-xhci,id=xhci",
        "usb-tablet,bus=xhci.0",
    )


def test_windows_argv_skips_kernel_and_emits_firmware_pflash(tmp_path: Path) -> None:
    """No direct-kernel artefacts; OVMF split-pflash drives appear."""
    spec = _fake_windows_spec()
    vm_info = _windows_vm_info(tmp_path)
    cmd = build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="",
        platform_spec=spec,
        firmware_vars_path=Path("/state/vm-win/OVMF_VARS.fd"),
        swtpm_socket=Path("/state/vm-win/swtpm-sock"),
        host_system="Linux",
    )

    # Windows is firmware-only — no direct-kernel artefacts.
    assert "-kernel" not in cmd
    assert "-append" not in cmd
    assert "-initrd" not in cmd

    # Both pflash drives (code = readonly, vars = writable).
    drive_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-drive"]
    pflash_drives = [d for d in drive_args if "if=pflash" in d]
    assert len(pflash_drives) == 2
    code_drive, vars_drive = pflash_drives
    assert "readonly=on" in code_drive
    assert "OVMF_CODE_4M.secboot.fd" in code_drive
    assert "readonly=on" not in vars_drive
    assert "/state/vm-win/OVMF_VARS.fd" in vars_drive


def test_windows_argv_emits_smm_machine_and_hyperv_cpu(tmp_path: Path) -> None:
    """The machine sub-options and CPU flags Windows needs."""
    spec = _fake_windows_spec()
    cmd = build_qemu_argv(
        _windows_vm_info(tmp_path),
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="",
        platform_spec=spec,
        firmware_vars_path=Path("/state/OVMF_VARS.fd"),
        swtpm_socket=Path("/state/swtpm-sock"),
        host_system="Linux",
    )

    machine_arg = cmd[cmd.index("-machine") + 1]
    for option in ("q35", "accel=kvm", "smm=on", "vmport=off", "kernel-irqchip=on"):
        assert option in machine_arg, f"missing {option!r} in machine arg: {machine_arg!r}"

    cpu_arg = cmd[cmd.index("-cpu") + 1]
    for flag in ("host", "hv_relaxed", "hv_vapic", "hv_spinlocks=0x1fff"):
        assert flag in cpu_arg, f"missing {flag!r} in cpu arg: {cpu_arg!r}"

    global_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-global"]
    assert any("cfi.pflash01" in g for g in global_args)
    assert any("ICH9-LPC.disable_s3=1" in g for g in global_args)


def test_windows_argv_emits_virtio_scsi_root_and_tpm(tmp_path: Path) -> None:
    """Root disk on virtio-scsi (with scsi-hd) and tpm-crb device."""
    spec = _fake_windows_spec()
    cmd = build_qemu_argv(
        _windows_vm_info(tmp_path),
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="",
        platform_spec=spec,
        firmware_vars_path=Path("/state/OVMF_VARS.fd"),
        swtpm_socket=Path("/state/swtpm-sock"),
        host_system="Linux",
    )

    device_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-device"]
    assert "virtio-scsi-pci,id=scsi0" in device_args
    assert "scsi-hd,bus=scsi0.0,drive=rootdisk0-drive" in device_args
    # NO legacy virtio-blk-pci attachment for the root disk.
    assert not any(d.startswith("virtio-blk-pci,drive=rootdisk0") for d in device_args)
    # USB topology: controller first, then tablet binding to it.
    assert device_args.index("qemu-xhci,id=xhci") < device_args.index("usb-tablet,bus=xhci.0")
    # TPM CRB device.
    assert "tpm-crb,tpmdev=tpm0" in device_args
    # And the chardev + tpmdev tokens.
    assert "-tpmdev" in cmd
    assert "-chardev" in cmd


def test_windows_argv_omits_rtc_override(tmp_path: Path) -> None:
    """Windows reads the RTC as local time, so the UTC override is skipped —
    QEMU's localtime default is left in place."""
    cmd = build_qemu_argv(
        _windows_vm_info(tmp_path),
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="",
        platform_spec=_fake_windows_spec(),
        firmware_vars_path=Path("/state/OVMF_VARS.fd"),
        swtpm_socket=Path("/state/swtpm-sock"),
        host_system="Linux",
    )
    assert "-rtc" not in cmd


def test_windows_argv_missing_firmware_vars_path_raises(tmp_path: Path) -> None:
    """Caller must provide a per-VM OVMF_VARS path when spec has firmware."""
    spec = _fake_windows_spec()
    with pytest.raises(SmolVMError, match="OVMF_VARS"):
        build_qemu_argv(
            _windows_vm_info(tmp_path),
            qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
            boot_args="",
            platform_spec=spec,
            firmware_vars_path=None,
            swtpm_socket=Path("/state/swtpm-sock"),
            host_system="Linux",
        )


def test_windows_argv_missing_swtpm_socket_raises(tmp_path: Path) -> None:
    """Caller must provide an swtpm socket when the spec requires TPM."""
    spec = _fake_windows_spec()
    with pytest.raises(SmolVMError, match="swtpm"):
        build_qemu_argv(
            _windows_vm_info(tmp_path),
            qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
            boot_args="",
            platform_spec=spec,
            firmware_vars_path=Path("/state/OVMF_VARS.fd"),
            swtpm_socket=None,
            host_system="Linux",
        )


def _tap_vm_info(tmp_path: Path, *, ssh_host_port: int | None = None) -> VMInfo:
    """A Linux QEMU VMInfo wired for host-TAP networking."""
    base = _qemu_vm_info(tmp_path)
    config = base.config.model_copy(update={"qemu_network": "tap"})
    network = base.network.model_copy(
        update={
            "tap_device": "tap5",
            "guest_ip": "172.16.0.5",
            "ssh_host_port": ssh_host_port,
        }
    )
    return base.model_copy(update={"config": config, "network": network})


def test_build_qemu_argv_tap_mode_emits_tap_netdev(tmp_path: Path) -> None:
    """qemu_network='tap' attaches the host TAP and drops slirp/hostfwd."""
    vm = _tap_vm_info(tmp_path, ssh_host_port=2200)
    cmd = build_qemu_argv(
        vm,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="console=ttyS0 root=/dev/vda rw init=/init",
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )
    joined = " ".join(cmd)
    assert "tap,id=net0,ifname=tap5,script=no,downscript=no" in joined
    assert "virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56" in joined
    # No userspace slirp NAT and no host port forwarding in tap mode.
    assert "user,id=net0" not in joined
    assert "hostfwd=" not in joined


def test_build_qemu_argv_tap_mode_allows_no_ssh_host_port(tmp_path: Path) -> None:
    """TAP mode does not require a reserved ssh_host_port (vsock control plane)."""
    vm = _tap_vm_info(tmp_path, ssh_host_port=None)
    # Must not raise despite ssh_host_port being None.
    cmd = build_qemu_argv(
        vm,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="console=ttyS0 root=/dev/vda rw init=/init",
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )
    assert "tap,id=net0,ifname=tap5,script=no,downscript=no" in " ".join(cmd)


def test_build_qemu_argv_slirp_still_requires_ssh_host_port(tmp_path: Path) -> None:
    """Default (slirp) mode keeps requiring a reserved ssh_host_port."""
    base = _qemu_vm_info(tmp_path)
    network = base.network.model_copy(update={"ssh_host_port": None})
    vm = base.model_copy(update={"network": network})
    with pytest.raises(SmolVMError, match="ssh_host_port"):
        build_qemu_argv(
            vm,
            qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
            boot_args="console=ttyS0 root=/dev/vda rw",
            platform_spec=_LINUX_SPEC,
            host_system="Linux",
        )
