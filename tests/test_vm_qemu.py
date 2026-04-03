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

from smolvm.types import PortForwardConfig, VMConfig
from smolvm.vm import SmolVMManager


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
    with patch.object(SmolVMManager, "_convert_qemu_managed_disk") as mock_convert:
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
    with patch.object(SmolVMManager, "_convert_qemu_managed_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.write_text("managed-qcow2")
        vm_info = sdk.create(config)

    expected_disk = sdk.data_dir / "disks" / "vm-qemu2.qcow2"
    assert vm_info.config.rootfs_path == expected_disk
    assert expected_disk.read_text() == "managed-qcow2"


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
    with patch.object(SmolVMManager, "_convert_qemu_managed_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.write_text("managed-qcow2")
        sdk.create(config)

    disk_path = sdk.data_dir / "disks" / "vm-qemu3.qcow2"
    sdk.delete("vm-qemu3")

    assert disk_path.exists()
