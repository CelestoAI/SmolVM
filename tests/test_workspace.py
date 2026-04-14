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

"""Tests for the ``--workspace`` (virtio-9p + overlayfs) feature."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from smolvm.types import VMConfig, WorkspaceMount
from smolvm.vm import SmolVMManager

# ── WorkspaceMount validation ───────────────────────────────────────


class TestWorkspaceMountValidation:
    """Tests for WorkspaceMount Pydantic model."""

    def test_valid_directory(self, tmp_path: Path) -> None:
        ws = WorkspaceMount(host_path=tmp_path)
        assert ws.host_path == tmp_path.resolve()
        assert ws.guest_path == "/workspace"
        assert ws.mount_tag is None

    def test_nonexistent_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="does not exist"):
            WorkspaceMount(host_path=tmp_path / "no-such-dir")

    def test_file_not_directory_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "afile.txt"
        f.touch()
        with pytest.raises(ValidationError, match="not a directory"):
            WorkspaceMount(host_path=f)

    def test_relative_guest_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="absolute path"):
            WorkspaceMount(host_path=tmp_path, guest_path="relative/path")

    def test_custom_guest_path(self, tmp_path: Path) -> None:
        ws = WorkspaceMount(host_path=tmp_path, guest_path="/mnt/data")
        assert ws.guest_path == "/mnt/data"

    def test_custom_mount_tag(self, tmp_path: Path) -> None:
        ws = WorkspaceMount(host_path=tmp_path, mount_tag="myshare")
        assert ws.mount_tag == "myshare"

    def test_resolved_tag_with_explicit_tag(self, tmp_path: Path) -> None:
        ws = WorkspaceMount(host_path=tmp_path, mount_tag="myshare")
        assert ws.resolved_tag(0) == "myshare"
        assert ws.resolved_tag(5) == "myshare"

    def test_resolved_tag_fallback(self, tmp_path: Path) -> None:
        ws = WorkspaceMount(host_path=tmp_path)
        assert ws.resolved_tag(0) == "workspace0"
        assert ws.resolved_tag(3) == "workspace3"


# ── VMConfig workspace_mounts validation ────────────────────────────


class TestVMConfigWorkspaceMounts:
    """Tests for workspace_mounts field on VMConfig."""

    def _make_config(self, tmp_path: Path, **kwargs: object) -> VMConfig:
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        return VMConfig(
            vm_id="ws-test",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            **kwargs,
        )

    def test_default_is_empty(self, tmp_path: Path) -> None:
        config = self._make_config(tmp_path)
        assert config.workspace_mounts == []

    def test_single_mount(self, tmp_path: Path) -> None:
        ws_dir = tmp_path / "project"
        ws_dir.mkdir()
        config = self._make_config(
            tmp_path,
            workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
        )
        assert len(config.workspace_mounts) == 1

    def test_duplicate_guest_path_rejected(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        with pytest.raises(ValidationError, match="Duplicate workspace guest_path"):
            self._make_config(
                tmp_path,
                workspace_mounts=[
                    WorkspaceMount(host_path=d1, guest_path="/workspace"),
                    WorkspaceMount(host_path=d2, guest_path="/workspace"),
                ],
            )

    def test_duplicate_mount_tag_rejected(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        with pytest.raises(ValidationError, match="Duplicate workspace mount tag"):
            self._make_config(
                tmp_path,
                workspace_mounts=[
                    WorkspaceMount(
                        host_path=d1, guest_path="/ws1", mount_tag="share",
                    ),
                    WorkspaceMount(
                        host_path=d2, guest_path="/ws2", mount_tag="share",
                    ),
                ],
            )


# ── QEMU command builder ────────────────────────────────────────────


@patch("smolvm.vm.subprocess.Popen")
@patch.object(
    SmolVMManager,
    "_find_qemu_binary",
    return_value=Path("/opt/homebrew/bin/qemu-system-aarch64"),
)
def test_start_qemu_includes_9p_workspace_args(
    _mock_find_qemu_binary: MagicMock,
    mock_popen: MagicMock,
    tmp_path: Path,
) -> None:
    """QEMU launch should include -fsdev and -device virtio-9p-device for workspaces."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id="vm-ws-test",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
        workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.touch()
        vm_info = sdk.create(config)

    proc = MagicMock()
    proc.pid = 12345
    mock_popen.return_value = proc

    with patch("smolvm.vm.platform.system", return_value="Darwin"):
        sdk._start_qemu(vm_info, tmp_path / "vm-ws-test.log")

    cmd = mock_popen.call_args.args[0]

    # Find the -fsdev argument
    fsdev_idx = cmd.index("-fsdev")
    fsdev_arg = cmd[fsdev_idx + 1]
    assert f"path={ws_dir.resolve()}" in fsdev_arg
    assert "security_model=mapped-xattr" in fsdev_arg
    assert "readonly=on" in fsdev_arg
    assert "id=fsdev-workspace0" in fsdev_arg

    # Find the virtio-9p device (aarch64 → virtio-9p-device)
    assert "virtio-9p-device,fsdev=fsdev-workspace0,mount_tag=workspace0" in cmd


# ── Firecracker rejection ───────────────────────────────────────────


@pytest.mark.parametrize("backend", ["firecracker", "libkrun"])
def test_workspace_rejected_on_non_qemu_backend(
    tmp_path: Path, backend: str,
) -> None:
    """Workspace mounts should be rejected for non-QEMU backends."""
    from smolvm.exceptions import SmolVMError

    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id=f"vm-{backend}-ws",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend=backend,
        workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend=backend,
    )

    with pytest.raises(SmolVMError, match="only supported with the QEMU"):
        sdk.create(config)


# ── Snapshot guard ──────────────────────────────────────────────────


def test_snapshot_rejected_with_workspace_mounts(tmp_path: Path) -> None:
    """Snapshotting should be blocked for VMs with workspace mounts."""
    from smolvm.exceptions import SmolVMError

    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id="vm-snap-ws",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
    )

    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.touch()
        vm_info = sdk.create(config)

    with pytest.raises(SmolVMError, match="workspace mounts"):
        sdk._ensure_snapshot_supported(vm_info)


# ── CLI ─────────────────────────────────────────────────────────────


class TestCliMountFlag:
    """Tests for the --mount CLI flag on create."""

    def test_single_mount_host_only(self) -> None:
        from smolvm.cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(["create", "--mount", "/tmp/project"])
        assert args.mounts == ["/tmp/project"]

    def test_single_mount_with_guest_path(self) -> None:
        from smolvm.cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(["create", "--mount", "/tmp/project:/code"])
        assert args.mounts == ["/tmp/project:/code"]

    def test_multiple_mounts(self) -> None:
        from smolvm.cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "create",
            "--mount", "/tmp/a",
            "--mount", "/tmp/b:/data",
        ])
        assert args.mounts == ["/tmp/a", "/tmp/b:/data"]

    def test_mount_defaults_to_none(self) -> None:
        from smolvm.cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(["create"])
        assert args.mounts is None


# ── Mount spec parsing ──────────────────────────────────────────────


class TestParseMountSpecs:
    """Tests for _parse_mount_specs helper."""

    def test_single_host_only_defaults_to_workspace(self, tmp_path: Path) -> None:
        from smolvm.facade import _parse_mount_specs

        mounts = _parse_mount_specs([str(tmp_path)])
        assert len(mounts) == 1
        assert mounts[0].host_path == tmp_path.resolve()
        assert mounts[0].guest_path == "/workspace"

    def test_host_with_guest_path(self, tmp_path: Path) -> None:
        from smolvm.facade import _parse_mount_specs

        mounts = _parse_mount_specs([f"{tmp_path}:/code"])
        assert len(mounts) == 1
        assert mounts[0].guest_path == "/code"

    def test_multiple_mounts_get_indexed_defaults(self, tmp_path: Path) -> None:
        from smolvm.facade import _parse_mount_specs

        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        mounts = _parse_mount_specs([str(d1), str(d2)])
        assert mounts[0].guest_path == "/workspace-0"
        assert mounts[1].guest_path == "/workspace-1"

    def test_mixed_explicit_and_default(self, tmp_path: Path) -> None:
        from smolvm.facade import _parse_mount_specs

        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        mounts = _parse_mount_specs([str(d1), f"{d2}:/data"])
        assert mounts[0].guest_path == "/workspace-0"
        assert mounts[1].guest_path == "/data"


# ── Facade guards ───────────────────────────────────────────────────


class TestFacadeWorkspaceGuards:
    """Tests for facade-level workspace mount guards."""

    def test_mount_workspaces_rejects_non_root_ssh(self, tmp_path: Path) -> None:
        """Workspace mounts should fail fast if ssh_user is not root."""
        from unittest.mock import MagicMock

        from smolvm.exceptions import SmolVMError
        from smolvm.facade import SmolVM

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        ws_dir = tmp_path / "project"
        ws_dir.mkdir()

        config = VMConfig(
            vm_id="vm-nonroot",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            ssh_capable=True,
            workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
        )

        with patch("smolvm.facade.SmolVMManager") as mock_sdk_cls:
            mock_sdk = MagicMock()
            mock_info = MagicMock(vm_id="vm-nonroot")
            mock_info.status = MagicMock()
            mock_info.status.value = "created"
            mock_info.config = config
            mock_sdk.create.return_value = mock_info

            running_info = MagicMock(vm_id="vm-nonroot")
            running_info.config = config
            running_info.network.guest_ip = "127.0.0.1"
            running_info.network.ssh_host_port = 2200
            mock_sdk.start.return_value = running_info
            mock_sdk_cls.return_value = mock_sdk

            vm = SmolVM(config, ssh_user="agent")
            with pytest.raises(SmolVMError, match="require ssh_user='root'"), \
                 patch.object(vm, "can_run_commands", return_value=True), \
                 patch.object(vm, "wait_for_ssh"), \
                 patch("smolvm.facade.SSHClient"):
                vm.start()
