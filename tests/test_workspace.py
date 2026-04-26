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

from smolvm.facade import SmolVM
from smolvm.types import CommandResult, VMConfig, VMState, WorkspaceMount
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

    def test_missing_host_path_loads_under_validate_paths_false(self, tmp_path: Path) -> None:
        """Persisted configs reload even when the host path was deleted.

        Read-only commands like ``smolvm list`` pass
        ``context={"validate_paths": False}`` so a stale mount path on disk
        does not crash the whole command. The validator must respect that.
        """
        ws_dir = tmp_path / "gone"
        ws_dir.mkdir()
        raw = WorkspaceMount(host_path=ws_dir).model_dump_json()
        ws_dir.rmdir()

        ws = WorkspaceMount.model_validate_json(raw, context={"validate_paths": False})

        assert ws.host_path == ws_dir.resolve()


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

    def test_persisted_config_reloads_with_missing_mount_host(self, tmp_path: Path) -> None:
        """Storage reads must succeed when a workspace host folder is gone.

        Reproduces the ``smolvm list`` crash where a deleted Conductor
        worktree caused the whole command to fail. The fix is that
        ``WorkspaceMount`` honors the ``validate_paths=False`` context the
        storage layer already passes via ``vm_config_from_json``.
        """
        ws_dir = tmp_path / "project"
        ws_dir.mkdir()
        config = self._make_config(
            tmp_path,
            workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
        )
        raw = config.model_dump_json()
        ws_dir.rmdir()

        reloaded = VMConfig.model_validate_json(raw, context={"validate_paths": False})

        assert len(reloaded.workspace_mounts) == 1
        assert reloaded.workspace_mounts[0].host_path == ws_dir.resolve()


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


@patch("smolvm.vm.subprocess.Popen")
@patch.object(
    SmolVMManager,
    "_find_qemu_binary",
    return_value=Path("/opt/homebrew/bin/qemu-system-aarch64"),
)
def test_start_friendly_error_when_workspace_host_path_missing(
    _mock_find_qemu_binary: MagicMock,
    mock_popen: MagicMock,
    tmp_path: Path,
) -> None:
    """`start` should refuse with a plain-English error when a mount's host
    folder has been deleted since the VM was created — no Pydantic stack."""
    from smolvm.exceptions import SmolVMError

    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id="vm-stale-mount",
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
        sdk.create(config)

    ws_dir.rmdir()  # simulate Conductor worktree cleanup

    with pytest.raises(SmolVMError, match="shared folder is missing") as exc_info:
        sdk.start("vm-stale-mount")
    # The error names the sandbox and the recovery command, so a first-time
    # user can act on it without reading the source.
    message = str(exc_info.value)
    assert "vm-stale-mount" in message
    assert "smolvm delete vm-stale-mount" in message
    mock_popen.assert_not_called()


@patch("smolvm.vm.subprocess.Popen")
@patch.object(
    SmolVMManager,
    "_find_qemu_binary",
    return_value=Path("/opt/homebrew/bin/qemu-system-aarch64"),
)
def test_start_friendly_error_when_workspace_host_path_is_a_file(
    _mock_find_qemu_binary: MagicMock,
    mock_popen: MagicMock,
    tmp_path: Path,
) -> None:
    """The preflight should also fire when the path now points to a file
    instead of a directory — covers the gap where the path technically
    exists but the original Pydantic ``is_dir()`` check would have
    rejected it. Without this we'd fall through to a backend error."""
    from smolvm.exceptions import SmolVMError

    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id="vm-mount-is-file",
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
        sdk.create(config)

    # Replace the directory with a file at the same path.
    ws_dir.rmdir()
    ws_dir.touch()

    with pytest.raises(SmolVMError, match="shared folder is missing"):
        sdk.start("vm-mount-is-file")
    mock_popen.assert_not_called()


@patch("smolvm.vm.subprocess.Popen")
@patch.object(
    SmolVMManager,
    "_find_qemu_binary",
    return_value=Path("/opt/homebrew/bin/qemu-system-aarch64"),
)
def test_async_start_runs_the_same_workspace_preflight(
    _mock_find_qemu_binary: MagicMock,
    mock_popen: MagicMock,
    tmp_path: Path,
) -> None:
    """``async_start`` is a parallel code path; if it skips the preflight
    the friendly error becomes a backend failure for async callers. The
    helper is shared so both surfaces give the same plain-English error.
    """
    import asyncio

    from smolvm.exceptions import SmolVMError

    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id="vm-async-stale",
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
        sdk.create(config)

    ws_dir.rmdir()

    with pytest.raises(SmolVMError, match="shared folder is missing"):
        asyncio.run(sdk.async_start("vm-async-stale"))
    mock_popen.assert_not_called()


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


# ── Mount auto-selects QEMU backend ─────────────────────────────────


@patch("smolvm.facade.SmolVMManager")
@patch("smolvm.facade._build_auto_config")
def test_mounts_without_backend_auto_selects_qemu(
    mock_build_auto_config: MagicMock,
    mock_sdk_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """`SmolVM(mounts=...)` without an explicit backend should pick QEMU so
    `--mount` works out of the box instead of erroring out on a non-QEMU
    default (Firecracker on Linux)."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    mock_build_auto_config.return_value = (
        VMConfig(
            vm_id="vm-auto",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
        ),
        None,
    )

    mock_sdk = MagicMock()
    mock_sdk.create.return_value = MagicMock(vm_id="vm-auto", status=VMState.CREATED)
    mock_sdk_cls.return_value = mock_sdk

    SmolVM(mounts=[str(ws_dir)])

    assert mock_build_auto_config.call_args.kwargs["backend"] == "qemu"
    # SmolVMManager must be initialized with the upgraded backend, not the
    # platform default.
    assert mock_sdk_cls.call_args.kwargs["backend"] == "qemu"


@patch("smolvm.facade.SmolVMManager")
def test_config_with_workspace_mounts_auto_selects_qemu(
    mock_sdk_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """`SmolVM(config=cfg)` with populated `cfg.workspace_mounts` and no
    backend pinned on either the config or the kwarg should upgrade the
    manager backend to QEMU."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    ws_dir = tmp_path / "project"
    ws_dir.mkdir()

    config = VMConfig(
        vm_id="vm-cfg-ws",
        kernel_path=kernel,
        rootfs_path=rootfs,
        workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
    )

    mock_sdk = MagicMock()
    mock_sdk.create.return_value = MagicMock(vm_id="vm-cfg-ws", status=VMState.CREATED)
    mock_sdk_cls.return_value = mock_sdk

    SmolVM(config)

    assert mock_sdk_cls.call_args.kwargs["backend"] == "qemu"


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

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade._build_auto_config")
    def test_create_with_mount_and_no_backend_selects_qemu(
        self,
        mock_build_auto_config: MagicMock,
        mock_smolvm_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """`smolvm create --mount /path` (no --backend) must auto-select QEMU
        at the CLI layer. Without this, _build_auto_config gets backend=None,
        resolves to the platform default (firecracker on Linux), and the
        downstream guard rejects the mount + firecracker combo with a
        confusing 'Re-run without --backend' message — the user already did."""
        from smolvm.cli.main import _run_create, build_parser

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        mock_build_auto_config.return_value = (
            VMConfig(
                vm_id="vm-mnt",
                kernel_path=kernel,
                rootfs_path=rootfs,
                backend="qemu",
            ),
            None,
        )
        mock_smolvm_cls.return_value.vm_id = "vm-mnt"
        mock_smolvm_cls.return_value.info.status = VMState.RUNNING

        parser = build_parser()
        args = parser.parse_args(
            ["create", "--mount", str(tmp_path / "project"), "--json"]
        )

        _run_create(args)

        assert args.backend == "qemu"
        assert mock_build_auto_config.call_args.kwargs["backend"] == "qemu"

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade._build_auto_config")
    def test_create_with_mount_and_explicit_firecracker_left_alone(
        self,
        mock_build_auto_config: MagicMock,
        mock_smolvm_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Explicit `--backend firecracker --mount /path` must NOT be silently
        upgraded; the downstream guard should still fire so the user sees the
        incompatibility they explicitly requested."""
        from smolvm.cli.main import _run_create, build_parser

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        mock_build_auto_config.return_value = (
            VMConfig(
                vm_id="vm-mnt",
                kernel_path=kernel,
                rootfs_path=rootfs,
                backend="firecracker",
            ),
            None,
        )
        mock_smolvm_cls.return_value.vm_id = "vm-mnt"
        mock_smolvm_cls.return_value.info.status = VMState.RUNNING

        parser = build_parser()
        args = parser.parse_args([
            "create",
            "--mount", str(tmp_path / "project"),
            "--backend", "firecracker",
            "--json",
        ])

        _run_create(args)

        assert args.backend == "firecracker"


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

    def test_mount_workspaces_repairs_ubuntu_missing_9p_modules(
        self,
        tmp_path: Path,
    ) -> None:
        """Ubuntu guests missing 9p modules should install linux-modules-extra."""
        ws_dir = tmp_path / "project"
        ws_dir.mkdir()
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        config = VMConfig(
            vm_id="vm-repair",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
        )

        vm = object.__new__(SmolVM)
        vm._vm_id = "vm-repair"
        vm._ssh_user = "root"
        vm._info = MagicMock(config=config)
        vm._ssh = MagicMock()
        vm._ssh.run.side_effect = [
            CommandResult(exit_code=1, stdout="", stderr="modprobe: FATAL: Module 9p"),
            CommandResult(exit_code=0, stdout="", stderr=""),
            CommandResult(exit_code=0, stdout="", stderr=""),
        ]

        vm._mount_workspaces()

        install_script = vm._ssh.run.call_args_list[1].args[0]
        mount_script = vm._ssh.run.call_args_list[2].args[0]
        assert "DPkg::Lock::Timeout=120" in install_script
        assert "Acquire::Retries=3" in install_script
        assert "fuser $APT_LOCKS" in install_script
        assert "linux-modules-extra-$(uname -r)" in install_script
        assert "mount -t 9p" in mount_script

    def test_mount_workspaces_reports_unrepairable_missing_9p(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-Ubuntu or unrepairable guests should fail before the mount loop."""
        from smolvm.exceptions import SmolVMError

        ws_dir = tmp_path / "project"
        ws_dir.mkdir()
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        config = VMConfig(
            vm_id="vm-unrepairable",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            workspace_mounts=[WorkspaceMount(host_path=ws_dir)],
        )

        vm = object.__new__(SmolVM)
        vm._vm_id = "vm-unrepairable"
        vm._ssh_user = "root"
        vm._info = MagicMock(config=config)
        vm._ssh = MagicMock()
        vm._ssh.run.side_effect = [
            CommandResult(exit_code=1, stdout="", stderr="modprobe: FATAL: Module 9p"),
            CommandResult(exit_code=42, stdout="", stderr="not ubuntu"),
        ]

        with pytest.raises(SmolVMError, match="missing 9p or overlay"):
            vm._mount_workspaces()

        assert vm._ssh.run.call_count == 2
