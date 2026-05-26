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

"""QEMU-specific SmolVM manager tests."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.types import NetworkConfig, PortForwardConfig, VMConfig, VMInfo, VMState
from smolvm.vm import SmolVMManager


def _qemu_vm_info(tmp_path: Path) -> VMInfo:
    """Return a minimal QEMU VMInfo for launch-command tests."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    return VMInfo(
        vm_id="vm-qemu",
        status=VMState.CREATED,
        config=VMConfig(
            vm_id="vm-qemu",
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


def test_start_qemu_missing_binary_uses_linux_install_hint(tmp_path: Path) -> None:
    """Linux users should get a Linux package-manager hint, not Homebrew."""
    sdk = SmolVMManager(data_dir=tmp_path / "data", socket_dir=tmp_path / "sockets", backend="qemu")

    with (
        patch.object(SmolVMManager, "_find_qemu_binary", return_value=None),
        patch("smolvm.vm.platform.system", return_value="Linux"),
        patch("smolvm.vm.platform.machine", return_value="x86_64"),
        patch("smolvm.vm._linux_os_release_ids", return_value={"ubuntu", "debian"}),
        pytest.raises(SmolVMError) as exc_info,
    ):
        sdk._start_qemu(_qemu_vm_info(tmp_path), tmp_path / "vm-qemu.log")

    message = str(exc_info.value)
    assert "sudo apt-get update && sudo apt-get install -y qemu-system-x86 qemu-utils" in message
    assert "brew install qemu" not in message


@patch("smolvm.vm.subprocess.Popen")
@patch.object(
    SmolVMManager,
    "_find_qemu_binary",
    return_value=Path("/opt/homebrew/bin/qemu-system-aarch64"),
)
def test_start_qemu_includes_configured_hostfwd_rules(
    _mock_find_qemu_binary: MagicMock,
    mock_popen: MagicMock,
    tmp_path: Path,
) -> None:
    """QEMU launch should include configured user-network host forwards."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    config = VMConfig(
        vm_id="vm-qemu1",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
        port_forwards=[
            PortForwardConfig(host_port=39011, guest_port=9222),
            PortForwardConfig(host_port=39012, guest_port=6080),
        ],
    )

    sdk = SmolVMManager(data_dir=tmp_path / "data", socket_dir=tmp_path / "sockets", backend="qemu")
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.touch()
        vm_info = sdk.create(config)

    proc = MagicMock()
    proc.pid = 12345
    mock_popen.return_value = proc

    with patch("smolvm.vm.platform.system", return_value="Darwin"):
        sdk._start_qemu(vm_info, tmp_path / "vm-qemu1.log")

    cmd = mock_popen.call_args.args[0]
    netdev_arg = cmd[cmd.index("-netdev") + 1]
    assert "hostfwd=tcp:127.0.0.1:2200-:22" in netdev_arg
    assert "hostfwd=tcp:127.0.0.1:39011-:9222" in netdev_arg
    assert "hostfwd=tcp:127.0.0.1:39012-:6080" in netdev_arg


@patch("smolvm.vm.subprocess.Popen")
@patch.object(
    SmolVMManager,
    "_find_qemu_binary",
    return_value=Path("/opt/homebrew/bin/qemu-system-aarch64"),
)
def test_start_qemu_uses_distinct_block_backend_and_node_names(
    _mock_find_qemu_binary: MagicMock,
    mock_popen: MagicMock,
    tmp_path: Path,
) -> None:
    """QEMU launch should not reuse the same name for backend id and block node."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    config = VMConfig(
        vm_id="vm-qemu-nodes",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
    )

    sdk = SmolVMManager(data_dir=tmp_path / "data", socket_dir=tmp_path / "sockets", backend="qemu")
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.touch()
        vm_info = sdk.create(config)

    proc = MagicMock()
    proc.pid = 12345
    mock_popen.return_value = proc

    with patch("smolvm.vm.platform.system", return_value="Darwin"):
        sdk._start_qemu(vm_info, tmp_path / "vm-qemu-nodes.log")

    cmd = mock_popen.call_args.args[0]
    drive_arg = cmd[cmd.index("-drive") + 1]
    assert "id=rootdisk0-drive" in drive_arg
    assert "node-name=rootdisk0" in drive_arg
    # Rootdisk must be the LAST virtio-blk-device on aarch64 (MMIO transport
    # enumerates devices in reverse declaration order; the kernel-side /dev/vda
    # ends up being whichever virtio-blk-device was added last).
    blk_devs = [a for a in cmd if isinstance(a, str) and a.startswith("virtio-blk-device,")]
    assert blk_devs[-1] == "virtio-blk-device,drive=rootdisk0-drive"


def test_create_qemu_uses_managed_qcow2_disk(tmp_path: Path) -> None:
    """QEMU isolated disks should be materialized as managed qcow2 files."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.write_text("rootfs-data")

    config = VMConfig(
        vm_id="vm-qemu2",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
    )

    sdk = SmolVMManager(data_dir=tmp_path / "data", socket_dir=tmp_path / "sockets", backend="qemu")
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.write_text("managed-qcow2")
        vm_info = sdk.create(config)

    expected_disk = sdk.data_dir / "disks" / "vm-qemu2.qcow2"
    assert vm_info.config.rootfs_path == expected_disk
    assert expected_disk.read_text() == "managed-qcow2"


def test_materialize_firmware_noop_for_linux_guests(tmp_path: Path) -> None:
    """Linux guests don't need per-VM OVMF NVRAM — _materialize_firmware is a no-op."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    config = VMConfig(
        vm_id="vm-linux",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    sdk._materialize_firmware(config)
    # No firmware dir should be created for Linux VMs.
    assert not (sdk.data_dir / "firmware" / "vm-linux").exists()


def test_materialize_firmware_copies_ovmf_template_for_windows(tmp_path: Path) -> None:
    """Windows guests get a per-VM OVMF_VARS.fd copied from the system template."""
    from smolvm.runtime.guest_platforms import FirmwareSpec
    from smolvm.types import GuestOS

    rootfs = tmp_path / "win11.qcow2"
    rootfs.touch()

    # Fake the system OVMF template — both files have to exist for our copy.
    template = tmp_path / "OVMF_VARS_4M.ms.fd"
    template.write_bytes(b"fake-ovmf-vars-template")
    code = tmp_path / "OVMF_CODE_4M.secboot.fd"
    code.touch()
    fake_spec_firmware = FirmwareSpec(code_path=code, vars_template_path=template)

    config = VMConfig(
        vm_id="vm-win",
        kernel_path=None,
        rootfs_path=rootfs,
        backend="qemu",
        guest_os=GuestOS.WINDOWS,
        boot_mode="firmware",
        disk_mode="shared",  # don't try to overlay a Windows qcow2
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    with patch(
        "smolvm.runtime.guest_platforms._find_x86_64_ovmf",
        return_value=fake_spec_firmware,
    ), patch("smolvm.vm.platform.system", return_value="Linux"), patch(
        "smolvm.vm.platform.machine", return_value="x86_64"
    ):
        sdk._materialize_firmware(config)

    target = sdk.data_dir / "firmware" / "vm-win" / "OVMF_VARS.fd"
    assert target.exists()
    assert target.read_bytes() == b"fake-ovmf-vars-template"
    # NVRAM is locked down (owner-only) by-default.
    assert (target.stat().st_mode & 0o777) == 0o600


def test_materialize_firmware_raises_with_install_hint_when_no_ovmf(tmp_path: Path) -> None:
    """Missing OVMF gives a plain-English install hint at create time."""
    from smolvm.types import GuestOS

    rootfs = tmp_path / "win11.qcow2"
    rootfs.touch()
    config = VMConfig(
        vm_id="vm-win-no-ovmf",
        kernel_path=None,
        rootfs_path=rootfs,
        backend="qemu",
        guest_os=GuestOS.WINDOWS,
        boot_mode="firmware",
        disk_mode="shared",
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    with patch(
        "smolvm.runtime.guest_platforms._find_x86_64_ovmf",
        return_value=None,
    ), patch("smolvm.vm.platform.system", return_value="Linux"), patch(
        "smolvm.vm.platform.machine", return_value="x86_64"
    ), pytest.raises(SmolVMError, match="OVMF"):
        sdk._materialize_firmware(config)


def test_windows_local_image_uses_per_vm_overlay_disk(tmp_path: Path) -> None:
    """Windows VMs get a per-VM overlay, baseline qcow2 stays untouched."""
    from smolvm.runtime.guest_platforms import FirmwareSpec
    from smolvm.types import GuestOS

    baseline = tmp_path / "win11-baseline.qcow2"
    baseline.write_bytes(b"baseline-bytes")
    baseline_mtime = baseline.stat().st_mtime_ns

    code = tmp_path / "OVMF_CODE.fd"
    code.touch()
    template = tmp_path / "OVMF_VARS.fd"
    template.write_bytes(b"vars-template")
    fake_firmware = FirmwareSpec(code_path=code, vars_template_path=template)

    config = VMConfig(
        vm_id="vm-win-iso",
        kernel_path=None,
        rootfs_path=baseline,
        backend="qemu",
        guest_os=GuestOS.WINDOWS,
        boot_mode="firmware",
        disk_mode="isolated",  # Phase 3a: new default for Windows local-image
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    with (
        patch(
            "smolvm.runtime.guest_platforms._find_x86_64_ovmf",
            return_value=fake_firmware,
        ),
        patch("smolvm.vm.platform.system", return_value="Linux"),
        patch("smolvm.vm.platform.machine", return_value="x86_64"),
        patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_overlay,
    ):
        mock_overlay.side_effect = lambda source, target: target.write_text("overlay-bytes")
        vm_info = sdk.create(config)

    expected_overlay = sdk.data_dir / "disks" / "vm-win-iso.qcow2"
    # The VM now points at its own overlay, NOT the user's baseline.
    assert vm_info.config.rootfs_path == expected_overlay
    assert expected_overlay.read_text() == "overlay-bytes"
    # The user's baseline file is byte-identical AND mtime-identical —
    # nothing wrote to it, so two SmolVMs in parallel can share it safely.
    assert baseline.read_bytes() == b"baseline-bytes"
    assert baseline.stat().st_mtime_ns == baseline_mtime
    # The overlay was created with the baseline as the backing file.
    source_arg, target_arg = mock_overlay.call_args.args
    assert source_arg == baseline
    assert target_arg == expected_overlay


def test_two_windows_vms_from_same_baseline_get_distinct_overlays(
    tmp_path: Path,
) -> None:
    """Concurrent Windows sandboxes from the same image use separate overlays."""
    from smolvm.runtime.guest_platforms import FirmwareSpec
    from smolvm.types import GuestOS

    baseline = tmp_path / "win11-baseline.qcow2"
    baseline.write_bytes(b"baseline-bytes")

    code = tmp_path / "OVMF_CODE.fd"
    code.touch()
    template = tmp_path / "OVMF_VARS.fd"
    template.write_bytes(b"vars-template")
    fake_firmware = FirmwareSpec(code_path=code, vars_template_path=template)

    def _windows_config(vm_id: str) -> VMConfig:
        return VMConfig(
            vm_id=vm_id,
            kernel_path=None,
            rootfs_path=baseline,
            backend="qemu",
            guest_os=GuestOS.WINDOWS,
            boot_mode="firmware",
            disk_mode="isolated",
        )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    with (
        patch(
            "smolvm.runtime.guest_platforms._find_x86_64_ovmf",
            return_value=fake_firmware,
        ),
        patch("smolvm.vm.platform.system", return_value="Linux"),
        patch("smolvm.vm.platform.machine", return_value="x86_64"),
        patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_overlay,
    ):
        mock_overlay.side_effect = lambda source, target: target.write_text(target.name)
        vm_alpha = sdk.create(_windows_config("vm-win-alpha"))
        vm_beta = sdk.create(_windows_config("vm-win-beta"))

    # Distinct per-VM overlays.
    assert vm_alpha.config.rootfs_path != vm_beta.config.rootfs_path
    assert vm_alpha.config.rootfs_path.name == "vm-win-alpha.qcow2"
    assert vm_beta.config.rootfs_path.name == "vm-win-beta.qcow2"
    # Both overlays share the same backing file (the user's baseline).
    backing_paths = [call.args[0] for call in mock_overlay.call_args_list]
    assert backing_paths == [baseline, baseline]


def test_delete_qemu_retains_isolated_disk_when_enabled(tmp_path: Path) -> None:
    """retain_disk_on_delete should preserve managed qcow2 disks for QEMU VMs."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.write_text("rootfs-data")

    config = VMConfig(
        vm_id="vm-qemu3",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
        retain_disk_on_delete=True,
    )

    sdk = SmolVMManager(data_dir=tmp_path / "data", socket_dir=tmp_path / "sockets", backend="qemu")
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.write_text("managed-qcow2")
        sdk.create(config)

    disk_path = sdk.data_dir / "disks" / "vm-qemu3.qcow2"
    sdk.delete("vm-qemu3")

    assert disk_path.exists()
