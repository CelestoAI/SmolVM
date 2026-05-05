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

"""Tests for SmolVM CLI commands."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from smolvm.cli.main import (
    DASHBOARD_ALLOW_BETA_ENV,
    _current_version_is_prerelease,
    build_parser,
    main,
)
from smolvm.types import BrowserSessionState, NetworkConfig, VMState, WorkspaceMount


def _make_vm_info(
    vm_id: str = "vm-abc123",
    status: VMState = VMState.RUNNING,
    guest_ip: str = "172.16.0.2",
    ssh_host_port: int | None = 2200,
    pid: int | None = 12345,
    workspace_mounts: list[WorkspaceMount] | None = None,
) -> MagicMock:
    """Build a lightweight VMInfo-like mock for list tests."""
    vm = MagicMock()
    vm.vm_id = vm_id
    vm.status = status
    vm.pid = pid
    vm.config.workspace_mounts = workspace_mounts or []
    if guest_ip:
        vm.network = MagicMock(spec=NetworkConfig)
        vm.network.guest_ip = guest_ip
        vm.network.ssh_host_port = ssh_host_port
    else:
        vm.network = None
    return vm


def _make_vm_with_stale_mount(
    tmp_path: Path,
    *,
    vm_id: str = "vm-abc123",
    status: VMState = VMState.RUNNING,
) -> tuple[MagicMock, Path]:
    """Build a VMInfo mock whose workspace mount points at a now-deleted folder.

    The mount is a real ``WorkspaceMount`` (not a loose ``MagicMock()``) so
    if ``WorkspaceMount`` ever renames its public attributes, these tests
    fail loudly instead of silently spoofing the API.

    Returns ``(vm_info_mock, missing_host_path)``.
    """
    ws_dir = tmp_path / f"{vm_id}-deleted-worktree"
    ws_dir.mkdir()
    mount = WorkspaceMount(host_path=ws_dir)
    ws_dir.rmdir()
    vm = _make_vm_info(vm_id, status, workspace_mounts=[mount])
    return vm, mount.host_path


def _make_snapshot_info(
    snapshot_id: str = "snap-001",
    vm_id: str = "vm001",
    *,
    backend: str = "firecracker",
    restored: bool = False,
    restored_vm_id: str | None = None,
) -> MagicMock:
    """Build a lightweight SnapshotInfo-like mock for CLI tests."""
    snapshot = MagicMock()
    snapshot.snapshot_id = snapshot_id
    snapshot.vm_id = vm_id
    snapshot.backend = backend
    snapshot.restored = restored
    snapshot.restored_vm_id = restored_vm_id
    snapshot.created_at = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
    snapshot.artifacts = MagicMock()
    snapshot.artifacts.state_path = Path(f"/tmp/{snapshot_id}/vmstate.bin")
    snapshot.artifacts.memory_path = Path(f"/tmp/{snapshot_id}/mem.bin")
    snapshot.artifacts.disk_path = Path(f"/tmp/{snapshot_id}/disk.ext4")
    return snapshot


def test_top_level_help_mentions_json_for_agents() -> None:
    """Top-level help should describe the machine-readable JSON mode."""
    help_text = build_parser().format_help()

    assert "--json" in help_text
    assert "machine-readable output" in help_text
    assert "LLMs, agents, and automation" in help_text


def test_create_help_describes_backend_specific_guest_default(
    capsys: pytest.CaptureFixture,
) -> None:
    """Create help should describe the OS option and its auto-detected default."""
    with pytest.raises(SystemExit) as exc_info:
        main(["create", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "Operating system image" in help_text
    assert "auto-detected" in help_text


class TestCliEnv:
    """Tests for `smolvm env` subcommands."""

    @pytest.fixture
    def mock_vm_cls(self) -> MagicMock:
        with patch("smolvm.facade.SmolVM") as m:
            yield m

    def _setup_vm(self, mock_vm_cls: MagicMock, vm_id: str = "vm001") -> MagicMock:
        vm = MagicMock()
        vm.vm_id = vm_id
        mock_vm_cls.from_id.return_value = vm
        return vm

    def test_env_set_success(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env set` success path."""
        vm = self._setup_vm(mock_vm_cls)
        vm.set_env_vars.return_value = ["FOO"]

        ret = main(["env", "set", "vm001", "FOO=bar"])

        assert ret == 0
        mock_vm_cls.from_id.assert_called_once_with(
            "vm001",
            ssh_user="root",
            ssh_key_path=None,
        )
        vm.set_env_vars.assert_called_once_with({"FOO": "bar"})
        vm.close.assert_called_once()
        assert "Set 1 env var(s)" in capsys.readouterr().out

    def test_env_set_multiple(
        self,
        mock_vm_cls: MagicMock,
    ) -> None:
        """Test `smolvm env set` with multiple variables."""
        vm = self._setup_vm(mock_vm_cls)
        vm.set_env_vars.return_value = ["A", "B"]

        ret = main(["env", "set", "vm001", "A=1", "B=2"])

        assert ret == 0
        vm.set_env_vars.assert_called_once_with({"A": "1", "B": "2"})

    def test_env_set_malformed_pair_fails(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test execution fails on malformed key=value pair."""
        ret = main(["env", "set", "vm001", "BADPAIR"])

        assert ret == 1
        mock_vm_cls.from_id.assert_not_called()
        assert "malformed pair" in capsys.readouterr().err

    def test_env_unset_success(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env unset` success path."""
        vm = self._setup_vm(mock_vm_cls)
        vm.unset_env_vars.return_value = {"FOO": "bar"}

        ret = main(["env", "unset", "vm001", "FOO"])

        assert ret == 0
        vm.unset_env_vars.assert_called_once_with(["FOO"])
        assert "Removed 1 env var(s)" in capsys.readouterr().out

    def test_env_list_success(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env list` success path (masked by default)."""
        vm = self._setup_vm(mock_vm_cls)
        vm.list_env_vars.return_value = {"FOO": "bar", "SECRET": "xyz"}

        ret = main(["env", "list", "vm001"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "FOO" in out
        assert "SECRET" in out
        assert "****" in out
        assert "bar" not in out  # Values hidden

    def test_env_list_show_values(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env list --show-values` reveals values."""
        vm = self._setup_vm(mock_vm_cls)
        vm.list_env_vars.return_value = {"FOO": "bar"}

        ret = main(["env", "list", "vm001", "--show-values"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "FOO" in out
        assert "bar" in out

    def test_env_set_json(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm env set --json` should emit the shared envelope."""
        vm = self._setup_vm(mock_vm_cls)
        vm.set_env_vars.return_value = ["FOO"]

        ret = main(["env", "set", "vm001", "FOO=bar", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "env.set"
        assert payload["ok"] is True
        assert payload["data"]["vm_id"] == "vm001"
        assert payload["data"]["requested_keys"] == ["FOO"]
        assert payload["data"]["present_keys"] == ["FOO"]
        assert "source /etc/profile.d/smolvm_env.sh" in payload["data"]["reload_hint"]

    def test_env_unset_json(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm env unset --json` should emit removed and missing keys."""
        vm = self._setup_vm(mock_vm_cls)
        vm.unset_env_vars.return_value = {"FOO": "bar"}

        ret = main(["env", "unset", "vm001", "FOO", "MISSING", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "env.unset"
        assert payload["data"]["removed_keys"] == ["FOO"]
        assert payload["data"]["missing_keys"] == ["MISSING"]

    def test_env_list_json_masked(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm env list --json` should mask values by default."""
        vm = self._setup_vm(mock_vm_cls)
        vm.list_env_vars.return_value = {"FOO": "bar"}

        ret = main(["env", "list", "vm001", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "env.list"
        assert payload["data"]["masked"] is True
        assert payload["data"]["variables"] == {"FOO": "****"}

    def test_env_list_json_show_values(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm env list --json --show-values` should reveal values."""
        vm = self._setup_vm(mock_vm_cls)
        vm.list_env_vars.return_value = {"FOO": "bar"}

        ret = main(["env", "list", "vm001", "--show-values", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["masked"] is False
        assert payload["data"]["variables"] == {"FOO": "bar"}

    def test_explicit_ssh_key_args(
        self,
        mock_vm_cls: MagicMock,
    ) -> None:
        """Test passing explicit SSH key and user via CLI args."""
        vm = self._setup_vm(mock_vm_cls)
        vm.list_env_vars.return_value = {}

        main(
            [
                "env",
                "list",
                "vm001",
                "--ssh-key",
                "/custom/key",
                "--ssh-user",
                "custom-user",
            ]
        )

        mock_vm_cls.from_id.assert_called_once_with(
            "vm001",
            ssh_user="custom-user",
            ssh_key_path="/custom/key",
        )

    def test_vm_lookup_failure_prints_error(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test handling of VM lookup failure."""
        mock_vm_cls.from_id.side_effect = Exception("VM not found")

        ret = main(["env", "list", "missing-vm"])

        assert ret == 1
        assert "Error: VM not found" in capsys.readouterr().err

    def test_vm_no_network_prints_error(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test handling of env operation failure from facade."""
        vm = self._setup_vm(mock_vm_cls)
        vm.list_env_vars.side_effect = Exception("VM has no network configuration")

        ret = main(["env", "list", "vm001"])

        assert ret == 1
        assert "no network configuration" in capsys.readouterr().err
        vm.close.assert_called_once()


class TestCliFile:
    """Tests for `smolvm file` subcommands."""

    @pytest.fixture
    def mock_vm_cls(self) -> MagicMock:
        with patch("smolvm.facade.SmolVM") as m:
            yield m

    def test_file_upload_success(
        self,
        mock_vm_cls: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm file upload` should copy a local file into a sandbox."""
        source = tmp_path / "note.txt"
        source.write_text("hello")
        vm = MagicMock()
        vm.upload_file.return_value = "/tmp/note.txt"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["file", "upload", "vm001", str(source), "/tmp/"])

        assert ret == 0
        mock_vm_cls.from_id.assert_called_once_with(
            "vm001",
            ssh_user="root",
            ssh_key_path=None,
        )
        vm.upload_file.assert_called_once_with(
            str(source),
            "/tmp/",
            make_dirs=True,
        )
        vm.close.assert_called_once()
        assert "Uploaded" in capsys.readouterr().out

    def test_file_upload_json(
        self,
        mock_vm_cls: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm file upload --json` should emit the upload destination."""
        source = tmp_path / "note.txt"
        source.write_text("hello")
        vm = MagicMock()
        vm.upload_file.return_value = "/tmp/note.txt"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["file", "upload", "vm001", str(source), "/tmp/", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "file.upload"
        assert payload["ok"] is True
        assert payload["data"]["vm_id"] == "vm001"
        assert payload["data"]["local_path"] == str(source)
        assert payload["data"]["guest_path"] == "/tmp/note.txt"

    def test_file_upload_can_skip_directory_creation(
        self,
        mock_vm_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """`--no-create-dirs` should pass make_dirs=False."""
        source = tmp_path / "note.txt"
        source.write_text("hello")
        vm = MagicMock()
        vm.upload_file.return_value = "/tmp/note.txt"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["file", "upload", "vm001", str(source), "/tmp/note.txt", "--no-create-dirs"])

        assert ret == 0
        vm.upload_file.assert_called_once_with(
            str(source),
            "/tmp/note.txt",
            make_dirs=False,
        )

    def test_file_upload_closes_vm_on_failure(
        self,
        mock_vm_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """If `upload_file` raises, the CLI must still close the VM and return nonzero."""
        source = tmp_path / "note.txt"
        source.write_text("hello")
        vm = MagicMock()
        vm.upload_file.side_effect = RuntimeError("boom")
        mock_vm_cls.from_id.return_value = vm

        ret = main(["file", "upload", "vm001", str(source), "/tmp/"])

        assert ret != 0
        vm.close.assert_called_once()


class TestCliCreate:
    """Tests for `smolvm create`."""

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.runtime.backends.platform.system", return_value="Darwin")
    def test_create_auto_generated_name(
        self,
        _: MagicMock,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm create` should auto-generate a VM name when omitted."""
        monkeypatch.delenv("SMOLVM_BACKEND", raising=False)
        config = MagicMock(vm_id="vm-a1b2c3d4")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")

        vm = MagicMock()
        vm.vm_id = "vm-a1b2c3d4"
        vm.info.config.backend = "qemu"
        vm.info.network = MagicMock(spec=NetworkConfig)
        vm.info.network.guest_ip = "172.16.0.2"
        vm.info.network.ssh_host_port = 2200
        mock_vm_cls.return_value = vm

        ret = main(["create"])

        assert ret == 0
        mock_build_auto_config.assert_called_once_with(
            vm_name=None,
            os=None,
            backend=None,
            memory=None,
            disk_size_mib=4096,
            ssh_key_path=None,
            on_download=ANY,
        )
        mock_vm_cls.assert_called_once_with(
            config,
            ssh_key_path="/tmp/id_ed25519",
            mounts=None,
            writable_mounts=False,
        )
        vm.start.assert_called_once_with(boot_timeout=30.0, on_progress=ANY)
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0, on_progress=ANY)
        vm.close.assert_called_once()
        out = capsys.readouterr().out
        assert "Created VM 'vm-a1b2c3d4'." in out
        assert "OS" in out
        assert "ubuntu" in out
        assert "Started" in out
        assert "smolvm ssh vm-a1b2c3d4" in out
        assert "smolvm info vm-a1b2c3d4" in out
        assert "Backend" not in out
        assert "IP Address" not in out
        assert "SSH Port" not in out

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.runtime.backends.platform.system", return_value="Darwin")
    def test_create_success(
        self,
        _: MagicMock,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm create` should build, start, and report a named VM."""
        monkeypatch.delenv("SMOLVM_BACKEND", raising=False)
        config = MagicMock(vm_id="project-spacex")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")

        vm = MagicMock()
        vm.vm_id = "project-spacex"
        vm.info.config.backend = "qemu"
        vm.info.network = MagicMock(spec=NetworkConfig)
        vm.info.network.guest_ip = "172.16.0.2"
        vm.info.network.ssh_host_port = 2200
        mock_vm_cls.return_value = vm

        ret = main(
            [
                "create",
                "--name",
                "project-spacex",
                "--memory",
                "1024",
                "--disk-size",
                "2048",
                "--backend",
                "qemu",
                "--boot-timeout",
                "45",
            ]
        )

        assert ret == 0
        mock_build_auto_config.assert_called_once_with(
            vm_name="project-spacex",
            os=None,
            backend="qemu",
            memory=1024,
            disk_size_mib=2048,
            ssh_key_path=None,
            on_download=ANY,
        )
        mock_vm_cls.assert_called_once_with(
            config,
            ssh_key_path="/tmp/id_ed25519",
            mounts=None,
            writable_mounts=False,
        )
        vm.start.assert_called_once_with(boot_timeout=45.0, on_progress=ANY)
        vm.wait_for_ssh.assert_called_once_with(timeout=45.0, on_progress=ANY)
        vm.stop.assert_not_called()
        vm.delete.assert_not_called()
        vm.close.assert_called_once()
        out = capsys.readouterr().out
        assert "Created VM 'project-spacex'." in out
        assert "OS" in out
        assert "ubuntu" in out
        assert "Started" in out
        assert "smolvm ssh project-spacex" in out
        assert "smolvm info project-spacex" in out
        assert "Backend" not in out
        assert "172.16.0.2" not in out
        assert "2200" not in out

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.runtime.backends.platform.system", return_value="Darwin")
    def test_create_success_with_short_name_flag(
        self,
        _: MagicMock,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`smolvm create -n ...` should behave the same as `--name`."""
        monkeypatch.delenv("SMOLVM_BACKEND", raising=False)
        config = MagicMock(vm_id="computer")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")

        vm = MagicMock()
        vm.vm_id = "computer"
        vm.info.config.backend = "qemu"
        vm.info.network = MagicMock(spec=NetworkConfig)
        vm.info.network.guest_ip = "172.16.0.2"
        vm.info.network.ssh_host_port = 2200
        mock_vm_cls.return_value = vm

        ret = main(["create", "-n", "computer"])

        assert ret == 0
        mock_build_auto_config.assert_called_once_with(
            vm_name="computer",
            os=None,
            backend=None,
            memory=None,
            disk_size_mib=4096,
            ssh_key_path=None,
            on_download=ANY,
        )
        mock_vm_cls.assert_called_once_with(
            config,
            ssh_key_path="/tmp/id_ed25519",
            mounts=None,
            writable_mounts=False,
        )
        vm.start.assert_called_once_with(boot_timeout=30.0, on_progress=ANY)
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0, on_progress=ANY)
        vm.close.assert_called_once()

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.runtime.backends.platform.system", return_value="Darwin")
    def test_create_json(
        self,
        _: MagicMock,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm create --json` should emit the shared envelope."""
        monkeypatch.delenv("SMOLVM_BACKEND", raising=False)
        config = MagicMock(vm_id="project-spacex")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")

        vm = MagicMock()
        vm.vm_id = "project-spacex"
        vm.info.config.backend = "qemu"
        vm.info.network = MagicMock(spec=NetworkConfig)
        vm.info.network.guest_ip = "172.16.0.2"
        vm.info.network.ssh_host_port = 2200
        mock_vm_cls.return_value = vm

        ret = main(["create", "--name", "project-spacex", "--json"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "Preparing ubuntu operating system image" not in out
        payload = json.loads(out)
        assert payload["command"] == "create"
        assert payload["data"]["vm"]["name"] == "project-spacex"
        assert payload["data"]["vm"]["os"] == "ubuntu"
        assert payload["data"]["vm"]["started_at"]
        assert payload["data"]["next"]["ssh_command"] == "smolvm ssh project-spacex"
        assert payload["data"]["next"]["info_command"] == "smolvm info project-spacex"

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_create_alpine_does_not_get_disk_size_default(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 4096 MiB CLI default only applies to debian/ubuntu, not alpine."""
        monkeypatch.delenv("SMOLVM_BACKEND", raising=False)
        mock_build_auto_config.return_value = (MagicMock(vm_id="vm"), "/tmp/id_ed25519")
        vm = MagicMock()
        vm.vm_id = "vm"
        vm.info.config.backend = "firecracker"
        vm.info.network = MagicMock(spec=NetworkConfig)
        vm.info.network.guest_ip = "172.16.0.2"
        vm.info.network.ssh_host_port = 2200
        mock_vm_cls.return_value = vm

        ret = main(["create", "--os", "alpine", "--json"])

        assert ret == 0
        mock_build_auto_config.assert_called_once_with(
            vm_name=None,
            os="alpine",
            backend=None,
            memory=None,
            disk_size_mib=None,
            ssh_key_path=None,
        )
        vm.start.assert_called_once_with(boot_timeout=30.0)
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0)
        vm.close.assert_called_once()

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_create_duplicate_name_failure(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Duplicate VM names should fail cleanly."""
        mock_build_auto_config.return_value = (MagicMock(vm_id="project-spacex"), "/tmp/id_ed25519")
        mock_vm_cls.side_effect = Exception("VM 'project-spacex' already exists")

        ret = main(["create", "--name", "project-spacex"])

        assert ret == 1
        assert "already exists" in capsys.readouterr().err

    @patch("smolvm.facade._build_auto_config")
    def test_create_invalid_name_failure(
        self,
        mock_build_auto_config: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Invalid VM IDs should be reported to the user."""
        mock_build_auto_config.side_effect = Exception("1 validation error for VMConfig")

        ret = main(["create", "--name", "Project SpaceX"])

        assert ret == 1
        assert "validation error" in capsys.readouterr().err

    @patch("smolvm.facade._build_auto_config")
    def test_create_image_build_failure(
        self,
        mock_build_auto_config: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Image build failures should surface actionable output."""
        mock_build_auto_config.side_effect = Exception("Docker is required to build images")

        ret = main(["create", "--name", "project-spacex"])

        assert ret == 1
        assert "Docker is required" in capsys.readouterr().err

    def test_create_invalid_os_choice(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Argparse should reject unsupported guest OS values."""
        with pytest.raises(SystemExit) as exc_info:
            main(["create", "--os", "fedora"])

        assert exc_info.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


class TestCliCreateImage:
    """Tests for `smolvm create --image`."""

    def test_image_flag_parsed(self) -> None:
        """--image flag should be recognized by the parser."""
        parser = build_parser()
        args = parser.parse_args(["create", "--image", "s3://bucket/images/test/"])
        assert args.image == "s3://bucket/images/test/"
        assert args.os is None

    def test_image_and_os_mutually_exclusive(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Argparse should reject --image and --os together."""
        with pytest.raises(SystemExit) as exc_info:
            main(["create", "--image", "s3://bucket/img/", "--os", "alpine"])

        assert exc_info.value.code == 2
        assert "not allowed" in capsys.readouterr().err

    def test_image_with_name_and_memory(self) -> None:
        """--image should work alongside --name, --memory, and --disk-size."""
        parser = build_parser()
        args = parser.parse_args(
            [
                "create",
                "--image",
                "s3://bucket/img/",
                "--name",
                "my-vm",
                "--memory",
                "1024",
                "--disk-size",
                "2048",
            ]
        )
        assert args.image == "s3://bucket/img/"
        assert args.name == "my-vm"
        assert args.memory_mib == 1024
        assert args.disk_size_mib == 2048

    def test_image_with_disk_size_is_rejected(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--disk-size has no effect on prebuilt S3 images and must be rejected."""
        ret = main(
            [
                "create",
                "--image",
                "s3://bucket/img/",
                "--disk-size",
                "8192",
            ]
        )

        assert ret == 1
        err = capsys.readouterr().err
        assert "--disk-size is incompatible with --image" in err


class TestCliStop:
    """Tests for `smolvm stop`."""

    @patch("smolvm.facade.SmolVM")
    def test_stop_success(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm stop` should stop an existing VM and report the result."""
        vm = MagicMock()
        vm.vm_id = "vm001"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["stop", "vm001", "--timeout", "7"])

        assert ret == 0
        mock_vm_cls.from_id.assert_called_once_with("vm001")
        vm.stop.assert_called_once_with(timeout=7.0)
        vm.close.assert_called_once()
        out = capsys.readouterr().out
        assert "Stopped VM 'vm001'." in out
        assert "stopped" in out

    @patch("smolvm.facade.SmolVM")
    def test_stop_json(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm stop --json` should emit the shared envelope."""
        vm = MagicMock()
        vm.vm_id = "vm001"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["stop", "vm001", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "stop"
        assert payload["ok"] is True
        assert payload["data"]["vm"]["name"] == "vm001"
        assert payload["data"]["vm"]["status"] == "stopped"

    @patch("smolvm.facade.SmolVM")
    def test_stop_missing_vm_prints_error(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Missing VMs should surface a clean error."""
        mock_vm_cls.from_id.side_effect = Exception("VM 'missing' not found")

        ret = main(["stop", "missing"])

        assert ret == 1
        assert "VM 'missing' not found" in capsys.readouterr().err


class TestCliPauseResume:
    """Tests for `smolvm pause` and `smolvm resume`."""

    @patch("smolvm.facade.SmolVM")
    def test_pause_success(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm pause` should pause an existing VM and report the result."""
        vm = MagicMock()
        vm.vm_id = "vm001"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["pause", "vm001"])

        assert ret == 0
        mock_vm_cls.from_id.assert_called_once_with("vm001")
        vm.pause.assert_called_once_with()
        vm.close.assert_called_once()
        out = capsys.readouterr().out
        assert "Paused VM 'vm001'." in out
        assert "paused" in out

    @patch("smolvm.facade.SmolVM")
    def test_resume_json(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm resume --json` should emit the shared envelope."""
        vm = MagicMock()
        vm.vm_id = "vm001"
        mock_vm_cls.from_id.return_value = vm

        ret = main(["resume", "vm001", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "resume"
        assert payload["ok"] is True
        assert payload["data"]["vm"]["name"] == "vm001"
        assert payload["data"]["vm"]["status"] == "running"


class TestCliSnapshot:
    """Tests for `smolvm snapshot` subcommands."""

    @patch("smolvm.facade.SmolVM")
    def test_snapshot_create_success(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm snapshot create` should create a snapshot from an existing VM."""
        vm = MagicMock()
        vm.snapshot.return_value = _make_snapshot_info()
        mock_vm_cls.from_id.return_value = vm

        ret = main(["snapshot", "create", "vm001", "--snapshot-id", "snap-001"])

        assert ret == 0
        mock_vm_cls.from_id.assert_called_once_with("vm001")
        vm.snapshot.assert_called_once_with(snapshot_id="snap-001", resume_source=False)
        vm.close.assert_called_once()
        out = capsys.readouterr().out
        assert "Created snapshot 'snap-001'" in out

    @patch("smolvm.facade.SmolVM")
    def test_snapshot_create_json(
        self,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm snapshot create --json` should emit snapshot metadata."""
        vm = MagicMock()
        vm.snapshot.return_value = _make_snapshot_info()
        mock_vm_cls.from_id.return_value = vm

        ret = main(["snapshot", "create", "vm001", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "snapshot.create"
        assert payload["data"]["snapshot"]["snapshot_id"] == "snap-001"
        assert payload["data"]["snapshot"]["vm_id"] == "vm001"
        assert payload["data"]["snapshot"]["backend"] == "firecracker"
        assert payload["data"]["snapshot"]["artifacts"]["disk_path"].endswith("disk.ext4")

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.vm.SmolVMManager")
    def test_snapshot_restore_json(
        self,
        mock_sdk_cls: MagicMock,
        mock_vm_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm snapshot restore --json` should report both snapshot and VM state."""
        sdk = mock_sdk_cls.return_value
        sdk.__enter__.return_value = sdk
        sdk.__exit__.side_effect = lambda *args: sdk.close()
        sdk.get_snapshot.return_value = _make_snapshot_info(restored=True, restored_vm_id="vm001")

        vm = MagicMock()
        vm.vm_id = "vm001"
        vm.status = VMState.PAUSED
        vm.info = _make_vm_info("vm001", VMState.PAUSED, "172.16.0.2", 2200, 999)
        mock_vm_cls.from_snapshot.return_value = vm

        ret = main(["snapshot", "restore", "snap-001", "--json"])

        assert ret == 0
        mock_vm_cls.from_snapshot.assert_called_once_with(
            "snap-001",
            resume_vm=False,
            force=False,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "snapshot.restore"
        assert payload["data"]["snapshot"]["restored"] is True
        assert payload["data"]["snapshot"]["backend"] == "firecracker"
        assert payload["data"]["vm"]["name"] == "vm001"
        assert payload["data"]["vm"]["status"] == "paused"

    @patch("smolvm.vm.SmolVMManager")
    def test_snapshot_delete_success(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm snapshot delete` should delete snapshot metadata and files."""
        sdk = mock_sdk_cls.return_value
        sdk.__enter__.return_value = sdk
        sdk.__exit__.side_effect = lambda *args: sdk.close()
        sdk.get_snapshot.return_value = _make_snapshot_info()

        ret = main(["snapshot", "delete", "snap-001"])

        assert ret == 0
        sdk.get_snapshot.assert_called_once_with("snap-001")
        sdk.delete_snapshot.assert_called_once_with("snap-001")
        assert "Deleted snapshot 'snap-001'." in capsys.readouterr().out

    @patch("smolvm.vm.SmolVMManager")
    def test_snapshot_list_json(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm snapshot list --json` should emit snapshot rows."""
        sdk = mock_sdk_cls.return_value
        sdk.__enter__.return_value = sdk
        sdk.__exit__.side_effect = lambda *args: sdk.close()
        sdk.list_snapshots.return_value = [
            _make_snapshot_info(),
            _make_snapshot_info("snap-002", restored=True, restored_vm_id="vm001"),
        ]

        ret = main(["snapshot", "list", "--json"])

        assert ret == 0
        sdk.list_snapshots.assert_called_once_with(vm_id=None)
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "snapshot.list"
        assert payload["data"]["filters"] == {"vm_id": None}
        assert payload["data"]["snapshots"][0]["snapshot_id"] == "snap-001"
        assert payload["data"]["snapshots"][0]["backend"] == "firecracker"
        assert payload["data"]["snapshots"][1]["restored"] is True


class TestCliSSH:
    """Tests for `smolvm ssh`."""

    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_running_vm_launches_subprocess(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """`smolvm ssh` should attach to a running VM without restarting it."""
        vm = MagicMock()
        vm.status = VMState.RUNNING
        vm._ssh_direct_command.return_value = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            "2200",
            "-i",
            "/custom/key",
            "custom-user@127.0.0.1",
        ]
        mock_vm_cls.from_id.return_value = vm
        mock_run.return_value = MagicMock(returncode=0)

        ret = main(
            [
                "ssh",
                "vm001",
                "--ssh-user",
                "custom-user",
                "--ssh-key",
                "/custom/key",
                "--boot-timeout",
                "15",
            ]
        )

        assert ret == 0
        mock_vm_cls.from_id.assert_called_once_with(
            "vm001",
            ssh_user="custom-user",
            ssh_key_path="/custom/key",
        )
        vm.start.assert_not_called()
        vm.wait_for_ssh.assert_not_called()
        vm._ssh_direct_command.assert_called_once_with()
        mock_run.assert_called_once_with(vm._ssh_direct_command.return_value, check=False)
        vm.close.assert_called_once()

    @pytest.mark.parametrize("status", [VMState.CREATED, VMState.STOPPED])
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_auto_starts_created_or_stopped_vm(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
        status: VMState,
    ) -> None:
        """`smolvm ssh` should auto-start attachable non-running VMs."""
        vm = MagicMock()
        vm.status = status
        vm._ssh_attach_command.return_value = ["ssh", "root@127.0.0.1"]
        mock_vm_cls.from_id.return_value = vm
        mock_run.return_value = MagicMock(returncode=0)

        ret = main(["ssh", "vm001"])

        assert ret == 0
        vm.start.assert_called_once_with(boot_timeout=30.0)
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0)
        mock_run.assert_called_once_with(["ssh", "root@127.0.0.1"], check=False)

    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_resumes_paused_vm(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """`smolvm ssh` should resume paused VMs before attaching."""
        vm = MagicMock()
        vm.status = VMState.PAUSED
        vm._ssh_attach_command.return_value = ["ssh", "root@127.0.0.1"]
        mock_vm_cls.from_id.return_value = vm
        mock_run.return_value = MagicMock(returncode=0)

        ret = main(["ssh", "vm001"])

        assert ret == 0
        vm.resume.assert_called_once_with()
        vm.start.assert_not_called()
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0)
        mock_run.assert_called_once_with(["ssh", "root@127.0.0.1"], check=False)

    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_error_state_fails_fast(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """VMs in ERROR should not be auto-started or attached."""
        vm = MagicMock()
        vm.status = VMState.ERROR
        mock_vm_cls.from_id.return_value = vm

        ret = main(["ssh", "vm001"])

        assert ret == 1
        vm.start.assert_not_called()
        vm.wait_for_ssh.assert_not_called()
        mock_run.assert_not_called()
        vm.close.assert_called_once()
        assert "error state" in capsys.readouterr().err

    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_missing_vm_prints_error(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Missing VMs should surface a clean error."""
        mock_vm_cls.from_id.side_effect = Exception("VM 'missing' not found")

        ret = main(["ssh", "missing"])

        assert ret == 1
        mock_run.assert_not_called()
        assert "VM 'missing' not found" in capsys.readouterr().err

    @patch("smolvm.cli.main.subprocess.run", side_effect=FileNotFoundError)
    @patch("smolvm.facade.SmolVM")
    def test_ssh_missing_local_ssh_binary(
        self,
        mock_vm_cls: MagicMock,
        _: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Missing host ssh binary should produce an actionable error."""
        vm = MagicMock()
        vm.status = VMState.RUNNING
        vm._ssh_attach_command.return_value = ["ssh", "root@127.0.0.1"]
        mock_vm_cls.from_id.return_value = vm

        ret = main(["ssh", "vm001"])

        assert ret == 1
        assert "openssh-client" in capsys.readouterr().err
        vm.close.assert_called_once()

    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_propagates_child_exit_code(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Nonzero ssh child exit codes should be returned unchanged."""
        vm = MagicMock()
        vm.status = VMState.RUNNING
        vm._ssh_attach_command.return_value = ["ssh", "root@127.0.0.1"]
        mock_vm_cls.from_id.return_value = vm
        mock_run.return_value = MagicMock(returncode=255)

        ret = main(["ssh", "vm001"])

        assert ret == 255


class TestCliDoctor:
    """Tests for `smolvm doctor`."""

    @patch("smolvm.cli.main.run_doctor")
    def test_doctor_default(self, mock_run_doctor: MagicMock) -> None:
        """Default doctor invocation should call run_doctor with defaults."""
        mock_run_doctor.return_value = 0

        ret = main(["doctor"])

        assert ret == 0
        mock_run_doctor.assert_called_once_with(
            backend=None,
            json_output=False,
            strict=False,
        )

    @patch("smolvm.cli.main.run_doctor")
    def test_doctor_with_flags(self, mock_run_doctor: MagicMock) -> None:
        """Doctor flags should be forwarded to run_doctor."""
        mock_run_doctor.return_value = 1

        ret = main(["doctor", "--backend", "firecracker", "--json", "--strict"])

        assert ret == 1
        mock_run_doctor.assert_called_once_with(
            backend="firecracker",
            json_output=True,
            strict=True,
        )


class TestCliSetup:
    """Tests for `smolvm setup` CLI wiring."""

    @patch("smolvm.cli.main._run_setup")
    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    def test_setup_dispatches_to_runner(
        self,
        mock_platform_system: MagicMock,
        mock_run_setup: MagicMock,
    ) -> None:
        """`smolvm setup` should dispatch through the setup handler."""
        mock_run_setup.return_value = 0

        ret = main(["setup"])

        assert ret == 0
        mock_run_setup.assert_called_once()

    @patch("smolvm.cli.main.platform.system", return_value="Darwin")
    def test_setup_rejects_linux_only_flags_on_macos(
        self,
        mock_platform_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Linux-only setup flags should fail at argparse time on macOS."""
        with pytest.raises(SystemExit) as exc_info:
            main(["setup", "--runtime-user", "foo"])

        assert exc_info.value.code == 2
        assert mock_platform_system.called
        err = capsys.readouterr().err
        assert "only supported on Linux" in err
        assert "Firecracker/KVM" in err

    @patch("smolvm.cli.main._run_setup")
    @patch("smolvm.cli.main.platform.system", return_value="Darwin")
    def test_setup_skip_deps_accepted_on_macos(
        self,
        mock_platform_system: MagicMock,
        mock_run_setup: MagicMock,
    ) -> None:
        """``--skip-deps`` is cross-platform and should be accepted on macOS."""
        mock_run_setup.return_value = 0

        ret = main(["setup", "--skip-deps"])

        assert ret == 0
        mock_run_setup.assert_called_once()

    @patch("smolvm.cli.main.platform.system", return_value="Windows")
    def test_setup_rejects_linux_only_flags_on_unsupported_os(
        self,
        mock_platform_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Linux-only flags should be rejected on any non-Linux platform."""
        with pytest.raises(SystemExit) as exc_info:
            main(["setup", "--no-configure-runtime"])

        assert exc_info.value.code == 2
        assert "only supported on Linux" in capsys.readouterr().err

    @patch("smolvm.cli.main.platform.system", return_value="Darwin")
    def test_setup_help_hides_linux_only_flags_on_macos(
        self,
        mock_platform_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Linux-only flags should not appear in ``--help`` on macOS."""
        with pytest.raises(SystemExit) as exc_info:
            main(["setup", "--help"])
        assert exc_info.value.code == 0

        help_text = capsys.readouterr().out

        # Linux-only flags should be hidden
        assert "--runtime-user" not in help_text
        assert "--remove-runtime-config" not in help_text
        assert "--no-configure-runtime" not in help_text
        # Cross-platform flags should still appear
        assert "--skip-deps" in help_text

    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    def test_setup_remove_runtime_config_conflicts_with_other_modes(
        self,
        mock_platform_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Removal mode should reject provisioning flags via argparse."""
        with pytest.raises(SystemExit) as exc_info:
            main(["setup", "--remove-runtime-config", "--with-docker"])

        assert exc_info.value.code == 2
        assert mock_platform_system.called
        assert "not allowed with --with-docker" in capsys.readouterr().err

    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch("smolvm.host.setup.run_setup")
    def test_setup_for_bake_forwards_options(
        self,
        mock_run_setup: MagicMock,
        mock_platform_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--for-bake`` should populate the bake-mode SetupOptions fields."""
        mock_run_setup.return_value = 0

        ret = main(["setup", "--for-bake", "--runtime-user", "ubuntu"])

        assert ret == 0
        mock_run_setup.assert_called_once()
        options = mock_run_setup.call_args.args[0]
        assert options.for_bake is True
        assert options.runtime_user == "ubuntu"
        # User-facing notice about doctor follow-up.
        assert "smolvm doctor" in capsys.readouterr().out

    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch("smolvm.host.setup.run_setup")
    def test_setup_firecracker_version_forwarded(
        self,
        mock_run_setup: MagicMock,
        mock_platform_system: MagicMock,
    ) -> None:
        """``--firecracker-version`` should populate SetupOptions.firecracker_version."""
        mock_run_setup.return_value = 0

        ret = main(["setup", "--firecracker-version", "v1.15.0"])

        assert ret == 0
        options = mock_run_setup.call_args.args[0]
        assert options.firecracker_version == "v1.15.0"

    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch("smolvm.host.setup.run_setup")
    def test_setup_assets_dir_prints_path_without_running_bash(
        self,
        mock_run_setup: MagicMock,
        mock_platform_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--assets-dir`` should print the asset root and exit 0 without invoking bash."""
        ret = main(["setup", "--assets-dir"])

        assert ret == 0
        mock_run_setup.assert_not_called()
        out = capsys.readouterr().out.strip()
        assert out, "expected --assets-dir to print a path"
        # The printed path should contain the script we depend on.
        assert (Path(out) / "system-setup.sh").is_file() or (
            Path(out) / "system-setup-macos.sh"
        ).is_file()

    @patch("smolvm.cli.main.maybe_print_update_notice")
    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch("smolvm.host.setup.run_setup")
    def test_setup_assets_dir_suppresses_update_notice(
        self,
        mock_run_setup: MagicMock,
        mock_platform_system: MagicMock,
        mock_notice: MagicMock,
    ) -> None:
        """``--assets-dir`` output is consumed by scripts; nag must be suppressed."""
        ret = main(["setup", "--assets-dir"])

        assert ret == 0
        mock_notice.assert_called_once()
        assert mock_notice.call_args.kwargs.get("json_output") is True

    @patch("smolvm.cli.main.maybe_print_update_notice")
    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch("smolvm.host.setup.run_setup")
    def test_setup_without_assets_dir_does_not_suppress_update_notice(
        self,
        mock_run_setup: MagicMock,
        mock_platform_system: MagicMock,
        mock_notice: MagicMock,
    ) -> None:
        """Plain ``setup`` (no --assets-dir, no --json) leaves the nag enabled."""
        mock_run_setup.return_value = 0

        ret = main(["setup"])

        assert ret == 0
        mock_notice.assert_called_once()
        assert mock_notice.call_args.kwargs.get("json_output") is False


class TestCurrentVersionIsPrerelease:
    """Tests for _current_version_is_prerelease helper."""

    @patch("smolvm.cli.main.importlib.metadata.version", return_value="0.0.5.a1")
    def test_alpha_version_is_prerelease(self, _: MagicMock) -> None:
        """Alpha versions (e.g. 0.0.5.a1) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.main.importlib.metadata.version", return_value="0.0.5.dev1")
    def test_dev_version_is_prerelease(self, _: MagicMock) -> None:
        """Dev versions (e.g. 0.0.5.dev1) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True


class TestCliBrowser:
    """Tests for `smolvm browser` commands."""

    @patch("smolvm.browser.BrowserSession")
    def test_browser_start_json(
        self, mock_browser_cls: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        """`smolvm browser start --json` should emit machine-readable session details."""
        session = MagicMock()
        session.session_id = "browser-abc123"
        session.vm_id = "browser-abc123"
        session.status = BrowserSessionState.READY
        session.cdp_url = "http://127.0.0.1:39222"
        session.live_url = "http://127.0.0.1:36080/vnc.html"
        session.info.profile_id = None
        session.artifacts_dir = Path("/tmp/browser-abc123")
        mock_browser_cls.return_value = session

        ret = main(["browser", "start", "--json"])

        assert ret == 0
        mock_browser_cls.assert_called_once()
        session.start.assert_called_once_with(boot_timeout=30.0)
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "browser.start"
        assert payload["ok"] is True
        assert payload["data"]["session_id"] == "browser-abc123"
        assert payload["data"]["cdp_url"] == "http://127.0.0.1:39222"

    @patch("smolvm.browser.BrowserSession")
    def test_browser_start_live_shortcut(self, mock_browser_cls: MagicMock) -> None:
        """`smolvm browser start --live` should map to live mode."""
        session = MagicMock()
        session.session_id = "browser-abc123"
        session.vm_id = "browser-abc123"
        session.status = BrowserSessionState.READY
        session.cdp_url = "http://127.0.0.1:39222"
        session.live_url = "http://127.0.0.1:36080/vnc.html"
        session.info.profile_id = None
        session.artifacts_dir = Path("/tmp/browser-abc123")
        mock_browser_cls.return_value = session

        ret = main(["browser", "start", "--live", "--json"])

        assert ret == 0
        mock_browser_cls.assert_called_once()
        config = mock_browser_cls.call_args.args[0]
        assert config.mode == "live"
        session.start.assert_called_once_with(boot_timeout=30.0)

    @patch("smolvm.browser.BrowserSession")
    def test_browser_open_requires_live_url(
        self,
        mock_browser_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm browser open` should fail cleanly for headless sessions."""
        session = MagicMock()
        session.live_url = None
        mock_browser_cls.from_id.return_value = session

        ret = main(["browser", "open", "browser-abc123"])

        assert ret == 1
        assert "does not have a live_url" in capsys.readouterr().err

    @patch("smolvm.browser.BrowserSession")
    @patch("smolvm.vm.resolve_data_dir", return_value=Path("/tmp"))
    @patch("smolvm.storage.create_state_manager")
    def test_browser_stop_all(
        self,
        mock_state_manager_cls: MagicMock,
        _mock_resolve_data_dir: MagicMock,
        mock_browser_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm browser stop --all` should stop every persisted session."""
        state_manager = MagicMock()
        state_manager.list_browser_sessions.return_value = [
            MagicMock(session_id="browser-001"),
            MagicMock(session_id="browser-002"),
        ]
        mock_state_manager_cls.return_value = state_manager

        first_session = MagicMock()
        second_session = MagicMock()
        mock_browser_cls.from_id.side_effect = [first_session, second_session]

        ret = main(["browser", "stop", "--all"])

        assert ret == 0
        state_manager.list_browser_sessions.assert_called_once_with()
        assert mock_browser_cls.from_id.call_args_list[0].args == ("browser-001",)
        assert mock_browser_cls.from_id.call_args_list[1].args == ("browser-002",)
        first_session.stop.assert_called_once_with()
        second_session.stop.assert_called_once_with()
        first_session.close.assert_called_once_with()
        second_session.close.assert_called_once_with()
        assert "Stopped 2 browser session(s)." in capsys.readouterr().out

    @patch("smolvm.vm.resolve_data_dir", return_value=Path("/tmp"))
    @patch("smolvm.storage.create_state_manager")
    def test_browser_stop_all_empty(
        self,
        mock_state_manager_cls: MagicMock,
        _mock_resolve_data_dir: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm browser stop --all` should be a no-op when nothing is persisted."""
        state_manager = MagicMock()
        state_manager.list_browser_sessions.return_value = []
        mock_state_manager_cls.return_value = state_manager

        ret = main(["browser", "stop", "--all"])

        assert ret == 0
        state_manager.list_browser_sessions.assert_called_once_with()
        assert "No browser sessions found." in capsys.readouterr().out

    @patch("smolvm.vm.resolve_data_dir", return_value=Path("/tmp"))
    @patch("smolvm.storage.create_state_manager")
    def test_browser_list_json(
        self,
        mock_state_manager_cls: MagicMock,
        _mock_resolve_data_dir: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm browser list --json` should serialize stored sessions."""
        state_manager = MagicMock()
        session = MagicMock()
        session.session_id = "browser-abc123"
        session.vm_id = "browser-abc123"
        session.status = BrowserSessionState.READY
        session.cdp_url = "http://127.0.0.1:39222"
        session.live_url = "http://127.0.0.1:36080/vnc.html"
        session.profile_id = None
        state_manager.list_browser_sessions.return_value = [session]
        mock_state_manager_cls.return_value = state_manager

        ret = main(["browser", "list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "browser.list"
        assert payload["ok"] is True
        assert payload["data"]["filters"] == {"status": None}
        assert payload["data"]["sessions"][0]["session_id"] == "browser-abc123"
        assert payload["data"]["sessions"][0]["status"] == "ready"

    @patch("smolvm.cli.main.importlib.metadata.version", return_value="0.0.5b2")
    def test_beta_version_is_prerelease(self, _: MagicMock) -> None:
        """Beta versions (e.g. 0.0.5b2) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.main.importlib.metadata.version", return_value="0.0.5rc1")
    def test_rc_version_is_prerelease(self, _: MagicMock) -> None:
        """Release candidates (e.g. 0.0.5rc1) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.main.importlib.metadata.version", return_value="0.0.5")
    def test_stable_version_is_not_prerelease(self, _: MagicMock) -> None:
        """Stable versions (e.g. 0.0.5) should NOT be detected as pre-release."""
        assert _current_version_is_prerelease() is False

    @patch("smolvm.cli.main.importlib.metadata.version", return_value="1.2.3")
    def test_stable_semver_is_not_prerelease(self, _: MagicMock) -> None:
        """Stable semantic versions (e.g. 1.2.3) should NOT be detected as pre-release."""
        assert _current_version_is_prerelease() is False

    def test_package_not_found_returns_false(self) -> None:
        """PackageNotFoundError should be handled gracefully by returning False."""
        import importlib.metadata

        with patch(
            "smolvm.cli.main.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("smolvm"),
        ):
            assert _current_version_is_prerelease() is False


class TestCliUi:
    """Tests for `smolvm ui`."""

    @patch("smolvm.cli.main.importlib.import_module")
    def test_ui_defaults(self, mock_import: MagicMock) -> None:
        """`smolvm ui` should launch uvicorn with defaults."""
        mock_uvicorn = MagicMock()
        mock_import.return_value = mock_uvicorn

        ret = main(["ui"])

        assert ret == 0
        mock_import.assert_called_once_with("uvicorn")
        mock_uvicorn.run.assert_called_once_with(
            "smolvm.dashboard.server:app",
            host="127.0.0.1",
            port=8080,
        )

    @patch("smolvm.cli.main.importlib.import_module")
    def test_ui_custom_port(self, mock_import: MagicMock) -> None:
        """Custom host/port should be forwarded to uvicorn."""
        mock_uvicorn = MagicMock()
        mock_import.return_value = mock_uvicorn

        ret = main(["ui", "--host", "0.0.0.0", "--port", "9090"])

        assert ret == 0
        mock_uvicorn.run.assert_called_once_with(
            "smolvm.dashboard.server:app",
            host="0.0.0.0",
            port=9090,
        )

    @patch("smolvm.cli.main.importlib.import_module")
    def test_ui_allow_beta_sets_env(self, mock_import: MagicMock) -> None:
        """--allow-beta should set env flag while uvicorn starts."""
        mock_uvicorn = MagicMock()

        def _run(*args: object, **kwargs: object) -> None:
            assert os.environ.get(DASHBOARD_ALLOW_BETA_ENV) == "1"

        mock_uvicorn.run.side_effect = _run
        mock_import.return_value = mock_uvicorn

        os.environ.pop(DASHBOARD_ALLOW_BETA_ENV, None)
        ret = main(["ui", "--allow-beta"])

        assert ret == 0
        assert DASHBOARD_ALLOW_BETA_ENV not in os.environ

    @patch("smolvm.cli.main.importlib.import_module", side_effect=ImportError)
    def test_ui_missing_dependency(
        self,
        _: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Missing dashboard extras should return an actionable error."""
        ret = main(["ui"])

        assert ret == 1
        assert "smolvm[dashboard]" in capsys.readouterr().err

    @patch("smolvm.cli.main.importlib.import_module")
    def test_ui_invalid_port(
        self,
        mock_import: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Out-of-range ports should fail fast with usage error code."""
        mock_import.return_value = MagicMock()

        ret = main(["ui", "--port", "70000"])

        assert ret == 2
        assert "invalid port" in capsys.readouterr().err

    @patch("smolvm.cli.main.importlib.import_module")
    def test_ui_auto_beta_for_prerelease_version(
        self,
        mock_import: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Pre-release smolvm version should auto-enable beta UI assets."""
        monkeypatch.setattr("smolvm.cli.main._current_version_is_prerelease", lambda: True)
        mock_uvicorn = MagicMock()

        def _run(*args: object, **kwargs: object) -> None:
            assert os.environ.get(DASHBOARD_ALLOW_BETA_ENV) == "1"

        mock_uvicorn.run.side_effect = _run
        mock_import.return_value = mock_uvicorn

        os.environ.pop(DASHBOARD_ALLOW_BETA_ENV, None)
        ret = main(["ui"])

        assert ret == 0
        assert DASHBOARD_ALLOW_BETA_ENV not in os.environ
        assert "auto-enabled" in capsys.readouterr().out

    @patch("smolvm.cli.main.importlib.import_module")
    def test_ui_no_auto_beta_for_stable_version(
        self,
        mock_import: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Stable smolvm version should NOT auto-enable beta UI assets."""
        monkeypatch.setattr("smolvm.cli.main._current_version_is_prerelease", lambda: False)
        mock_uvicorn = MagicMock()
        mock_import.return_value = mock_uvicorn

        os.environ.pop(DASHBOARD_ALLOW_BETA_ENV, None)
        ret = main(["ui"])

        assert ret == 0
        assert DASHBOARD_ALLOW_BETA_ENV not in os.environ
        assert "auto-enabled" not in capsys.readouterr().out


class TestCliList:
    """Tests for `smolvm list`."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.vm.SmolVMManager") as m:
            m.return_value.__enter__.return_value = m.return_value
            m.return_value.__exit__.side_effect = lambda *args: m.return_value.close()
            yield m

    def test_list_empty(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list` with no running VMs should print a friendly message."""
        mock_sdk_cls.return_value.list_vms.return_value = []

        ret = main(["list"])

        assert ret == 0
        assert "No running VMs found." in capsys.readouterr().out
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=VMState.RUNNING)
        mock_sdk_cls.return_value.close.assert_called_once()

    def test_list_shows_vms(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list` should print a Rich table with name, status, and pid."""
        vms = [_make_vm_info("vm-abc123", VMState.RUNNING, "172.16.0.2", 2200, 12345)]
        mock_sdk_cls.return_value.list_vms.return_value = vms

        ret = main(["list"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "vm-abc123" in out
        assert "running" in out
        assert "12345" in out
        assert "SmolVM Instances" in out
        assert "Name" in out
        assert "Status" in out
        assert "PID" in out
        assert "Total: 1 VM(s)." in out
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=VMState.RUNNING)

    def test_list_all_shows_running_and_stopped_vms(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list --all` should include stopped VMs."""
        vms = [
            _make_vm_info("vm-abc123", VMState.RUNNING, "172.16.0.2", 2200, 12345),
            _make_vm_info("vm-def456", VMState.STOPPED, "172.16.0.3", None, None),
        ]
        mock_sdk_cls.return_value.list_vms.return_value = vms

        ret = main(["list", "--all"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "vm-abc123" in out
        assert "vm-def456" in out
        assert "stopped" in out
        assert "Total: 2 VM(s)." in out
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=None)

    def test_list_no_network(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list` should show '-' for a missing PID."""
        vms = [_make_vm_info("vm-abc123", VMState.CREATED, "", None, None)]
        vms[0].network = None
        mock_sdk_cls.return_value.list_vms.return_value = vms

        ret = main(["list"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "vm-abc123" in out
        assert "created" in out
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=VMState.RUNNING)
        assert "PID" in out
        assert "-" in out

    def test_list_status_filter(
        self,
        mock_sdk_cls: MagicMock,
    ) -> None:
        """`smolvm list --status running` passes status to list_vms."""
        mock_sdk_cls.return_value.list_vms.return_value = []

        ret = main(["list", "--status", "running"])

        assert ret == 0
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=VMState.RUNNING)

    def test_list_json(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list --json` should emit structured data for running VMs."""
        mock_sdk_cls.return_value.list_vms.return_value = [
            _make_vm_info("vm-abc123", VMState.RUNNING, "172.16.0.2", 2200, 12345),
        ]

        ret = main(["list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "list"
        assert payload["ok"] is True
        assert payload["data"]["filters"] == {"all": False, "status": "running"}
        assert payload["data"]["vms"] == [
            {
                "name": "vm-abc123",
                "status": "running",
                "ip_address": "172.16.0.2",
                "ssh_port": 2200,
                "pid": 12345,
                "warnings": [],
            }
        ]
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=VMState.RUNNING)

    def test_list_json_empty(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list --json` should emit an empty JSON array when nothing matches."""
        mock_sdk_cls.return_value.list_vms.return_value = []

        ret = main(["list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["vms"] == []
        assert payload["data"]["filters"] == {"all": False, "status": "running"}
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=VMState.RUNNING)

    def test_list_all_json(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list --all --json` should emit all VM rows."""
        mock_sdk_cls.return_value.list_vms.return_value = [
            _make_vm_info("vm-abc123", VMState.RUNNING, "172.16.0.2", 2200, 12345),
            _make_vm_info("vm-def456", VMState.STOPPED, "172.16.0.3", None, None),
        ]

        ret = main(["list", "--all", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["filters"] == {"all": True, "status": None}
        assert payload["data"]["vms"][0]["name"] == "vm-abc123"
        assert payload["data"]["vms"][1]["status"] == "stopped"
        assert payload["data"]["vms"][1]["ssh_port"] is None
        mock_sdk_cls.return_value.list_vms.assert_called_once_with(status=None)

    def test_list_status_filter_empty(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list --status stopped` with no results shows filtered message."""
        mock_sdk_cls.return_value.list_vms.return_value = []

        ret = main(["list", "--status", "stopped"])

        assert ret == 0
        assert "stopped" in capsys.readouterr().out

    def test_list_sdk_error(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm list` prints error and returns 1 on unexpected failure."""
        mock_sdk_cls.return_value.list_vms.side_effect = RuntimeError("db unavailable")

        ret = main(["list"])

        assert ret == 1
        assert "Error: db unavailable" in capsys.readouterr().err
        mock_sdk_cls.return_value.close.assert_called_once()

    def test_list_flags_stale_workspace_mount(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """`smolvm list` should keep listing VMs whose host mount is gone,
        and print a warning naming the missing path."""
        vm, missing = _make_vm_with_stale_mount(tmp_path)
        mock_sdk_cls.return_value.list_vms.return_value = [vm]

        ret = main(["list"])

        # Rich may wrap long tmp paths across lines; flatten before asserting.
        out = capsys.readouterr().out.replace("\n", "")
        assert ret == 0
        assert "vm-abc123" in out
        assert "Warnings:" in out
        assert str(missing) in out
        # The warning explains what to do, not just what's wrong.
        assert "smolvm delete vm-abc123" in out

    def test_list_warning_does_not_claim_running_sandbox_cannot_start(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """The warning must not falsely claim a running sandbox can't start.

        The user can SSH into a sandbox that was already running when its
        host folder got deleted — saying 'cannot start' contradicts what
        they're seeing. The chosen wording sidesteps the consequence
        entirely and just states the fact + the recovery.
        """
        vm, _ = _make_vm_with_stale_mount(tmp_path, vm_id="sbx-running")
        mock_sdk_cls.return_value.list_vms.return_value = [vm]

        ret = main(["list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        warning = payload["data"]["vms"][0]["warnings"][0]
        assert "cannot start" not in warning.lower()

    def test_list_json_includes_warnings(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """`smolvm list --json` should expose stale mounts via `warnings`."""
        vm, missing = _make_vm_with_stale_mount(tmp_path)
        mock_sdk_cls.return_value.list_vms.return_value = [vm]

        ret = main(["list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        warnings = payload["data"]["vms"][0]["warnings"]
        assert len(warnings) == 1
        # JSON consumers (agents) get the same self-contained message:
        # what's wrong, the missing path, and how to recover.
        assert str(missing) in warnings[0]
        assert "missing" in warnings[0]
        assert "smolvm delete vm-abc123" in warnings[0]


class TestCliInfo:
    """Tests for `smolvm info`."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.vm.SmolVMManager") as m:
            m.return_value.__enter__.return_value = m.return_value
            m.return_value.__exit__.side_effect = lambda *args: m.return_value.close()
            yield m

    @staticmethod
    def _make_info_vm(
        vm_id: str = "sbx-pauling",
        status: VMState = VMState.RUNNING,
        backend: str = "qemu",
        guest_ip: str | None = "10.0.2.15",
        ssh_host_port: int | None = 2200,
        pid: int | None = 4242,
        vcpus: int = 2,
        memory_mib: int = 1024,
        rootfs_path: Path | None = None,
        kernel_path: Path | None = None,
        initrd_path: Path | None = None,
    ) -> MagicMock:
        vm = MagicMock()
        vm.vm_id = vm_id
        vm.status = status
        vm.config.backend = backend
        vm.config.vcpu_count = vcpus
        vm.config.memory = memory_mib
        vm.config.rootfs_path = rootfs_path
        vm.config.kernel_path = kernel_path
        vm.config.initrd_path = initrd_path
        vm.pid = pid
        if guest_ip is not None:
            vm.network = MagicMock(spec=NetworkConfig)
            vm.network.guest_ip = guest_ip
            vm.network.ssh_host_port = ssh_host_port
        else:
            vm.network = None
        return vm

    def test_info_renders_full_table(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm info <name>` should show the full details table."""
        rootfs = tmp_path / "ubuntu-noble-minimal-qemu-x86_64" / "rootfs.qcow2"
        rootfs.parent.mkdir(parents=True)
        rootfs.write_bytes(b"\0" * (5 * 1024 * 1024))  # 5 MiB
        mock_sdk_cls.return_value.state.get_vm.return_value = self._make_info_vm(
            status=VMState.STOPPED, rootfs_path=rootfs
        )

        ret = main(["info", "sbx-pauling"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "sbx-pauling" in out
        assert "stopped" in out
        assert "qemu" in out
        assert "10.0.2.15" in out
        assert "2200" in out
        assert "4242" in out
        assert "CPUs" in out
        assert "Memory" in out
        assert "1024 MiB" in out
        assert "Disk Size" in out
        assert "5 MiB" in out
        assert "ubuntu" in out
        mock_sdk_cls.return_value.state.get_vm.assert_called_once_with("sbx-pauling")
        mock_sdk_cls.return_value.close.assert_called_once()

    def test_info_running_vm_queries_live_data(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """For running VMs, info should overlay OS and used memory from SSH."""
        vm_info = self._make_info_vm(status=VMState.RUNNING)
        mock_sdk_cls.return_value.state.get_vm.return_value = vm_info
        with patch("smolvm.cli.main._query_live_vm_info") as mock_query:
            mock_query.return_value = {
                "os": "Ubuntu 24.04.1 LTS",
                "memory_used": 312,
            }

            ret = main(["info", "sbx-pauling"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "Ubuntu 24.04.1 LTS" in out
        assert "312 / 1024 MiB used" in out
        mock_query.assert_called_once_with(vm_info)

    def test_info_running_vm_with_unreachable_ssh(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """If SSH probe fails, info should still render with placeholders."""
        mock_sdk_cls.return_value.state.get_vm.return_value = self._make_info_vm(
            status=VMState.RUNNING
        )
        with patch("smolvm.cli.main._query_live_vm_info") as mock_query:
            mock_query.return_value = {}

            ret = main(["info", "sbx-pauling"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "1024 MiB" in out
        # No "used" suffix when memory_used is unavailable.
        assert "used" not in out

    def test_info_handles_missing_network(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm info` should render '-' when the VM has no network."""
        mock_sdk_cls.return_value.state.get_vm.return_value = self._make_info_vm(
            status=VMState.STOPPED, guest_ip=None, ssh_host_port=None, pid=None
        )

        ret = main(["info", "sbx-pauling"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "stopped" in out
        assert "-" in out

    def test_info_json(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm info --json` should emit a structured envelope."""
        rootfs = tmp_path / "alpine-virt" / "rootfs.ext4"
        rootfs.parent.mkdir(parents=True)
        rootfs.write_bytes(b"\0" * (3 * 1024 * 1024))  # 3 MiB
        mock_sdk_cls.return_value.state.get_vm.return_value = self._make_info_vm(
            status=VMState.STOPPED, rootfs_path=rootfs
        )

        ret = main(["info", "sbx-pauling", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "info"
        assert payload["ok"] is True
        assert payload["data"]["vm"] == {
            "name": "sbx-pauling",
            "status": "stopped",
            "os": "alpine",
            "backend": "qemu",
            "ip_address": "10.0.2.15",
            "ssh_port": 2200,
            "pid": 4242,
            "vcpus": 2,
            "memory": 1024,
            "memory_used": None,
            "disk_size": 3,
        }

    def test_info_not_found(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm info` returns 1 and an error message when the VM is missing."""
        mock_sdk_cls.return_value.state.get_vm.side_effect = RuntimeError("VM 'ghost' not found")

        ret = main(["info", "ghost"])

        assert ret == 1
        assert "VM 'ghost' not found" in capsys.readouterr().err
        mock_sdk_cls.return_value.close.assert_called_once()

    def test_info_qcow2_uses_virtual_size(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """For qcow2 rootfs, disk size should report the guest-visible virtual size, not the host file footprint."""
        rootfs = tmp_path / "ubuntu" / "rootfs.qcow2"
        rootfs.parent.mkdir(parents=True)
        rootfs.write_bytes(b"\0" * (1 * 1024 * 1024))  # 1 MiB on disk
        mock_sdk_cls.return_value.state.get_vm.return_value = self._make_info_vm(
            status=VMState.STOPPED, rootfs_path=rootfs
        )
        with patch("smolvm.facade._qcow2_virtual_size_mib", return_value=8192) as mock_qsize:
            ret = main(["info", "sbx-pauling", "--json"])

        assert ret == 0
        mock_qsize.assert_called_once_with(rootfs)
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["vm"]["disk_size"] == 8192


class TestCliStart:
    """Tests for `smolvm <preset> start`."""

    def _make_vm_mock(self, vm_id: str = "sbx-codex") -> MagicMock:
        vm = MagicMock()
        vm.vm_id = vm_id
        vm.info.status = VMState.RUNNING
        vm.info.config.backend = "qemu"
        vm.info.network = MagicMock(spec=NetworkConfig)
        vm.info.network.guest_ip = "172.16.0.2"
        vm.info.network.ssh_host_port = 2200
        return vm

    def test_top_level_help_lists_known_presets(self, capsys: pytest.CaptureFixture) -> None:
        """`smolvm --help` should list every registered preset as a top-level command."""
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "codex" in out
        assert "claude-code" in out

    def test_preset_help_lists_start_action(self, capsys: pytest.CaptureFixture) -> None:
        """`smolvm codex --help` should list the `start` action."""
        with pytest.raises(SystemExit):
            main(["codex", "--help"])
        out = capsys.readouterr().out
        assert "start" in out

    def test_unknown_preset_errors(self, capsys: pytest.CaptureFixture) -> None:
        """An unknown preset name should fail at argparse-level."""
        with pytest.raises(SystemExit):
            main(["nonexistent-agent", "start"])
        err = capsys.readouterr().err
        # argparse produces "invalid choice" for unknown subcommand
        assert "invalid choice" in err or "argument command" in err

    def test_launch_snippet_runs_when_env_file_missing(self, tmp_path: Path) -> None:
        """The remote command built by `_exec_launch_command` must exec the
        harness even when /etc/profile.d/smolvm_env.sh does not exist —
        regression for claude-code with subscription auth where no
        ANTHROPIC_API_KEY is set on the host, so env injection writes
        nothing and the file is never created."""
        import subprocess

        from smolvm.cli.main import _exec_launch_command

        captured: list[list[str]] = []

        class _StubSshVm:
            def _ssh_attach_command(self) -> list[str]:
                return ["ssh", "-p", "2200", "root@127.0.0.1"]

        def fake_run(*args: object, **_kwargs: object) -> MagicMock:
            # Tolerate future kwargs (e.g. text=, env=) on the real
            # subprocess.run call without rewriting the stub.
            captured.append(args[0])  # type: ignore[arg-type]
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("smolvm.cli.main.subprocess.run", side_effect=fake_run):
            _exec_launch_command(_StubSshVm(), "claude")

        remote = captured[0][-1]
        # Now actually evaluate the remote snippet under bash with a
        # path that does not exist — the launch (here a `:` no-op
        # standing in for `exec claude`) must still execute.
        missing_env_file = tmp_path / "definitely-not-here.sh"
        # The snippet calls `exec claude`; for the runtime check we
        # substitute a benign command we can verify ran.
        snippet = remote.replace("exec claude", "echo LAUNCHED")
        snippet = snippet.replace("/etc/profile.d/smolvm_env.sh", str(missing_env_file))
        completed = subprocess.run(
            ["bash", "-c", snippet], capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0
        assert "LAUNCHED" in completed.stdout
        assert "No such file" not in completed.stderr

    def test_launch_snippet_prepends_local_bin_to_path(self, tmp_path: Path) -> None:
        """The launch snippet must prepend ``~/.local/bin`` to PATH so a
        harness that self-installed there (claude-code's npm postinstall
        migrates to ``~/.local/bin/claude``) is found by the non-login
        SSH shell, which otherwise inherits root's default PATH."""
        import subprocess

        from smolvm.cli.main import _exec_launch_command

        captured: list[list[str]] = []

        class _StubSshVm:
            def _ssh_attach_command(self) -> list[str]:
                return ["ssh", "-p", "2200", "root@127.0.0.1"]

        def fake_run(*args: object, **_kwargs: object) -> MagicMock:
            captured.append(args[0])  # type: ignore[arg-type]
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("smolvm.cli.main.subprocess.run", side_effect=fake_run):
            _exec_launch_command(_StubSshVm(), "claude")

        remote = captured[0][-1]
        # Drop a fake binary at $HOME/.local/bin/claude and verify the
        # snippet would resolve `claude` from there. We swap `exec` for a
        # `command -v` probe so the test stays in-process.
        home = tmp_path / "home"
        local_bin = home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "claude").write_text("#!/bin/sh\necho FROM_LOCAL_BIN\n")
        (local_bin / "claude").chmod(0o755)

        missing_env_file = tmp_path / "missing.sh"
        snippet = remote.replace("exec claude", "command -v claude")
        snippet = snippet.replace("/etc/profile.d/smolvm_env.sh", str(missing_env_file))
        completed = subprocess.run(
            ["bash", "-c", snippet],
            capture_output=True,
            text=True,
            check=False,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
        assert completed.returncode == 0
        assert str(local_bin / "claude") in completed.stdout

    def test_claude_alias_resolves_to_claude_code(self) -> None:
        """`smolvm claude start` should be accepted as an alias for
        `smolvm claude-code start` — first-time users keep typing the
        short name and the previous behaviour was an unfriendly
        argparse 'invalid choice' error."""
        parser = build_parser()
        args = parser.parse_args(["claude", "start"])
        # argparse stores whichever spelling the user typed in
        # ``args.command``, but the canonical preset name (set via
        # ``set_defaults``) is what the dispatch path looks up.
        assert args.command == "claude"
        assert args.preset_name == "claude-code"

    def test_top_level_help_lists_claude_alias(self, capsys: pytest.CaptureFixture) -> None:
        """The alias should appear in the top-level help so the
        shorthand is discoverable, not a hidden trick."""
        import re

        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        # Must check 'claude' as a distinct token, not a substring of
        # 'claude-code'. argparse renders the choices block as
        # ``{a,b,claude-code,claude,...}`` so split on whitespace and the
        # punctuation argparse uses there.
        tokens = set(re.split(r"[\s{},]+", out))
        assert "claude" in tokens
        assert "claude-code" in tokens

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main._apply_preset_with_progress")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_codex_default_path(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply: MagicMock,
        _mock_is_published: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm codex start` boots ubuntu/qemu with preset defaults and applies the preset.

        Forces the install-at-boot path (is_preset_published=False) since
        codex now has a published image and would otherwise take the fast
        path. The published-path coverage is exercised in separate tests.
        """
        from smolvm.types import GuestOS

        config = MagicMock(vm_id="sbx-codex")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        vm = self._make_vm_mock("sbx-codex")
        mock_vm_cls.return_value = vm
        mock_apply.return_value = {
            "preset": "codex",
            "copied_configs": ["/root/.codex"],
            "injected_env_keys": ["OPENAI_API_KEY"],
        }

        ret = main(["codex", "start", "--name", "sbx-codex"])

        assert ret == 0
        mock_build_auto_config.assert_called_once_with(
            vm_name="sbx-codex",
            name_prefix="codex",
            os=GuestOS.UBUNTU,
            backend="qemu",
            memory=2048,
            disk_size_mib=8192,
            ssh_key_path=None,
            on_download=ANY,
        )
        mock_vm_cls.assert_called_once_with(
            config,
            ssh_key_path="/tmp/id_ed25519",
            mounts=None,
            writable_mounts=False,
        )
        vm.start.assert_called_once_with(boot_timeout=30.0, on_progress=ANY)
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0, on_progress=ANY)
        mock_apply.assert_called_once()
        vm.close.assert_called_once()

        out = capsys.readouterr().out
        assert "sbx-codex" in out
        assert "codex" in out
        assert "OPENAI_API_KEY" in out
        assert "smolvm ssh sbx-codex" in out

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.presets.apply_preset")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_codex_json(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply_fn: MagicMock,
        _mock_is_published: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm codex start --json` should emit the start envelope."""
        config = MagicMock(vm_id="sbx-1")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        vm = self._make_vm_mock("sbx-1")
        mock_vm_cls.return_value = vm
        mock_apply_fn.return_value = {
            "preset": "codex",
            "copied_configs": [],
            "injected_env_keys": ["OPENAI_API_KEY"],
        }

        ret = main(["codex", "start", "--name", "sbx-1", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "start"
        assert payload["ok"] is True
        assert payload["data"]["vm"]["name"] == "sbx-1"
        assert payload["data"]["vm"]["os"] == "ubuntu"
        assert payload["data"]["preset"]["name"] == "codex"
        assert payload["data"]["preset"]["injected_env_keys"] == ["OPENAI_API_KEY"]
        assert payload["data"]["next"]["ssh_command"] == "smolvm ssh sbx-1"

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main._apply_preset_with_progress")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_claude_code_overrides_memory(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply: MagicMock,
        _mock_is_published: MagicMock,
    ) -> None:
        """User --memory should override the preset default."""
        config = MagicMock(vm_id="sbx")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        vm = self._make_vm_mock("sbx")
        mock_vm_cls.return_value = vm
        mock_apply.return_value = {
            "preset": "claude-code",
            "copied_configs": [],
            "injected_env_keys": [],
        }

        ret = main(["claude-code", "start", "--memory", "4096", "--disk-size", "16384"])

        assert ret == 0
        kwargs = mock_build_auto_config.call_args.kwargs
        assert kwargs["memory"] == 4096
        assert kwargs["disk_size_mib"] == 16384

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_rejects_non_qemu_backend(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        _mock_is_published: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Install-at-boot path rejects non-qemu backends.

        Forces is_preset_published=False so the install-at-boot fallback
        runs (codex now has firecracker/qemu/libkrun published images,
        which would otherwise take the fast path on Linux). The rejection
        only fires when neither path is available.
        """
        ret = main(["codex", "start", "--backend", "firecracker"])

        assert ret == 2
        err = capsys.readouterr().err
        assert "requires --backend qemu" in err
        # Nothing should have started.
        mock_build_auto_config.assert_not_called()
        mock_vm_cls.assert_not_called()

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.cli.main._apply_preset_with_progress")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_attach_runs_codex_via_ssh(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply: MagicMock,
        mock_subprocess_run: MagicMock,
        _mock_is_published: MagicMock,
    ) -> None:
        """`--attach` should ssh into the box and exec the launch command."""
        config = MagicMock(vm_id="sbx")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        vm = self._make_vm_mock("sbx")
        vm._ssh_attach_command.return_value = [
            "ssh",
            "-p",
            "2200",
            "root@127.0.0.1",
        ]
        mock_vm_cls.return_value = vm
        mock_apply.return_value = {
            "preset": "codex",
            "copied_configs": [],
            "injected_env_keys": ["OPENAI_API_KEY"],
        }
        completed = MagicMock()
        completed.returncode = 0
        mock_subprocess_run.return_value = completed

        ret = main(["codex", "start", "--attach"])

        assert ret == 0
        mock_subprocess_run.assert_called_once()
        cmd = mock_subprocess_run.call_args.args[0]
        # `-t` must come before user@host so OpenSSH allocates a TTY.
        assert "-t" in cmd
        assert cmd.index("-t") < cmd.index("root@127.0.0.1")
        # Remote command must guard the env-file source and still exec the
        # harness if the file is missing (preset may inject zero env vars).
        remote = cmd[-1]
        assert "/etc/profile.d/smolvm_env.sh" in remote
        assert remote.endswith("; exec codex"), (
            "exec must chain with ';' not '&&' so a missing env file does "
            f"not abort the launch — got {remote!r}"
        )
        assert "[ -r " in remote, "env file source must be guarded with a file-existence check"

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.cli.main._apply_preset_with_progress")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_no_attach_skips_subprocess(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply: MagicMock,
        mock_subprocess_run: MagicMock,
        _mock_is_published: MagicMock,
    ) -> None:
        """`--no-attach` should skip both the prompt and the ssh launch."""
        config = MagicMock(vm_id="sbx")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        mock_vm_cls.return_value = self._make_vm_mock("sbx")
        mock_apply.return_value = {
            "preset": "codex",
            "copied_configs": [],
            "injected_env_keys": [],
        }

        ret = main(["codex", "start", "--no-attach"])

        assert ret == 0
        mock_subprocess_run.assert_not_called()

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.cli.main.sys.stdin")
    @patch("builtins.input", return_value="y")
    @patch("smolvm.cli.main._apply_preset_with_progress")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_prompt_yes_attaches(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply: MagicMock,
        mock_input: MagicMock,
        mock_stdin: MagicMock,
        mock_subprocess_run: MagicMock,
        _mock_is_published: MagicMock,
    ) -> None:
        """Default behavior on a TTY: prompt; ``y`` answer attaches."""
        mock_stdin.isatty.return_value = True

        config = MagicMock(vm_id="sbx")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        vm = self._make_vm_mock("sbx")
        vm._ssh_attach_command.return_value = ["ssh", "root@127.0.0.1"]
        mock_vm_cls.return_value = vm
        mock_apply.return_value = {
            "preset": "codex",
            "copied_configs": [],
            "injected_env_keys": [],
        }
        completed = MagicMock()
        completed.returncode = 0
        mock_subprocess_run.return_value = completed

        ret = main(["codex", "start"])

        assert ret == 0
        mock_input.assert_called_once()
        mock_subprocess_run.assert_called_once()

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.cli.main.sys.stdin")
    @patch("builtins.input", return_value="n")
    @patch("smolvm.cli.main._apply_preset_with_progress")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_prompt_no_skips_attach(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply: MagicMock,
        mock_input: MagicMock,
        mock_stdin: MagicMock,
        mock_subprocess_run: MagicMock,
        _mock_is_published: MagicMock,
    ) -> None:
        """A ``n`` answer should skip the ssh launch."""
        mock_stdin.isatty.return_value = True

        config = MagicMock(vm_id="sbx")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        mock_vm_cls.return_value = self._make_vm_mock("sbx")
        mock_apply.return_value = {
            "preset": "codex",
            "copied_configs": [],
            "injected_env_keys": [],
        }

        ret = main(["codex", "start"])

        assert ret == 0
        mock_input.assert_called_once()
        mock_subprocess_run.assert_not_called()

    @patch("smolvm.images.published.is_preset_published", return_value=False)
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.presets.apply_preset")
    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_start_json_never_attaches(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        mock_apply_fn: MagicMock,
        mock_subprocess_run: MagicMock,
        _mock_is_published: MagicMock,
    ) -> None:
        """JSON mode should never prompt or attach, even when a launch command exists."""
        config = MagicMock(vm_id="sbx")
        mock_build_auto_config.return_value = (config, "/tmp/id_ed25519")
        mock_vm_cls.return_value = self._make_vm_mock("sbx")
        mock_apply_fn.return_value = {
            "preset": "codex",
            "copied_configs": [],
            "injected_env_keys": [],
        }

        ret = main(["codex", "start", "--json"])

        assert ret == 0
        mock_subprocess_run.assert_not_called()


class TestPublishedImageLaunchPath:
    """Tests for the published-image launch path.

    ``smolvm <preset> start`` uses a pre-built rootfs from GitHub Releases
    via ensure_published_image, then boots directly. Tooling assumed to be
    preinstalled in the image.
    """

    @patch("smolvm.cli.main.platform.machine")
    def test_arch_helper_normalizes(self, mock_machine: MagicMock) -> None:
        from smolvm.cli.main import _host_arch_for_published

        for raw, expected in [
            ("x86_64", "amd64"),
            ("amd64", "amd64"),
            ("AMD64", "amd64"),
            ("arm64", "arm64"),
            ("aarch64", "arm64"),
            ("ARM64", "arm64"),
        ]:
            mock_machine.return_value = raw
            assert _host_arch_for_published() == expected, raw

    @patch("smolvm.cli.main.platform.machine", return_value="riscv64")
    def test_arch_helper_rejects_unsupported(self, _mock_machine: MagicMock) -> None:
        from smolvm.cli.main import _host_arch_for_published

        with pytest.raises(RuntimeError, match="Unsupported host architecture"):
            _host_arch_for_published()

    @patch("smolvm.cli.main._run_start_with_published_image")
    def test_start_routes_to_published_path_when_env_set(
        self,
        mock_published_path: MagicMock,
    ) -> None:
        """Published path must short-circuit before the legacy install-at-boot path."""
        mock_published_path.return_value = 0

        ret = main(["openclaw", "start", "--json"])

        assert ret == 0
        mock_published_path.assert_called_once()
        # First positional is args, second is the resolved preset.
        called_args = mock_published_path.call_args[0]
        assert called_args[1].name == "openclaw"

    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch("smolvm.images.published.ensure_published_image")
    def test_published_path_surfaces_missing_manifest_error(
        self,
        mock_ensure: MagicMock,
        _mock_system: MagicMock,
    ) -> None:
        """An empty manifest entry should produce a clean CLI error, not a crash."""
        from smolvm.exceptions import ImageError

        mock_ensure.side_effect = ImageError(
            "No published image for preset 'openclaw' on arch 'amd64' (available: (none))."
        )

        ret = main(["openclaw", "start", "--json"])

        assert ret == 1
        mock_ensure.assert_called_once()

    @pytest.mark.parametrize(
        "system,expected_vmm",
        [
            ("Linux", "firecracker"),
            ("Darwin", "qemu"),
        ],
    )
    @patch("smolvm.cli.main.platform.system")
    def test_vmm_for_host_maps_os_to_kernel_variant(
        self,
        mock_system: MagicMock,
        system: str,
        expected_vmm: str,
    ) -> None:
        from smolvm.cli.main import _vmm_for_host

        mock_system.return_value = system
        assert _vmm_for_host() == expected_vmm

    @patch("smolvm.cli.main.platform.system", return_value="FreeBSD")
    def test_vmm_for_host_rejects_unsupported_os(self, _mock_system: MagicMock) -> None:
        from smolvm.cli.main import _vmm_for_host

        with pytest.raises(RuntimeError, match="Unsupported host OS"):
            _vmm_for_host()

    @pytest.mark.parametrize(
        "vmm,arch,expected_console",
        [
            ("qemu", "arm64", "console=ttyAMA0"),
            ("qemu", "amd64", "console=ttyS0"),
            ("libkrun", "arm64", "console=ttyAMA0"),
            ("libkrun", "amd64", "console=ttyS0"),
        ],
    )
    def test_boot_args_for_qemu_picks_console_per_arch(
        self,
        vmm: str,
        arch: str,
        expected_console: str,
    ) -> None:
        from smolvm.cli.main import _boot_args_for

        result = _boot_args_for("openclaw", vmm, arch)  # type: ignore[arg-type]
        assert expected_console in result
        assert "init=/init" in result

    def test_boot_args_for_firecracker_omits_console_arg(self) -> None:
        from smolvm.cli.main import _boot_args_for

        # Firecracker's base string already disables 8250 and uses its own
        # console wiring — no console= should be added by the helper.
        for arch in ("amd64", "arm64"):
            result = _boot_args_for("openclaw", "firecracker", arch)  # type: ignore[arg-type]
            assert "console=" not in result
            assert "8250.nr_uarts=0" in result

    @patch("smolvm.cli.main.platform.system", return_value="Linux")
    @patch(
        "smolvm.cli.main._PUBLISHED_IMAGE_BOOT_ARGS",
        new={},  # nothing registered → unconditional miss
    )
    def test_published_path_rejects_unconfigured_preset_vmm(
        self,
        _mock_system: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A preset with no boot_args entry for the resolved vmm must
        produce a clean exit-2 error, not a KeyError further down."""
        ret = main(["openclaw", "start", "--json"])

        envelope = json.loads(capsys.readouterr().out)
        assert ret == 2
        assert envelope["exit_code"] == 2
        assert "no boot_args" in envelope["error"]["message"]

    @pytest.mark.parametrize(
        "system,machine,expected_arch,expected_vmm,expected_backend",
        [
            ("Linux", "x86_64", "amd64", "firecracker", "firecracker"),
            ("Linux", "aarch64", "arm64", "firecracker", "firecracker"),
            ("Darwin", "arm64", "arm64", "qemu", "qemu"),
            ("Darwin", "x86_64", "amd64", "qemu", "qemu"),
        ],
    )
    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.images.published.ensure_published_image")
    @patch("smolvm.cli.main.platform.machine")
    @patch("smolvm.cli.main.platform.system")
    def test_published_path_happy_path_skips_apply_preset(
        self,
        mock_system: MagicMock,
        mock_machine: MagicMock,
        mock_ensure_image: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_vm_cls: MagicMock,
        _mock_subprocess: MagicMock,
        tmp_path: Path,
        system: str,
        machine: str,
        expected_arch: str,
        expected_vmm: str,
        expected_backend: str,
    ) -> None:
        """End-to-end: download → VMConfig → start, no apply_preset call."""
        from smolvm.images.manager import LocalImage

        mock_system.return_value = system
        mock_machine.return_value = machine

        kernel = tmp_path / "vmlinux.bin"
        rootfs = tmp_path / "rootfs.ext4"
        priv = tmp_path / "id_ed25519"
        pub = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        priv.touch()
        pub.write_text("ssh-ed25519 AAAAExampleKey user@host\n")

        mock_ensure_image.return_value = LocalImage(
            name=f"openclaw-v0.0.13-{expected_arch}-{expected_vmm}",
            kernel_path=kernel,
            rootfs_path=rootfs,
        )
        mock_ensure_ssh_key.return_value = (priv, pub)
        mock_vm = MagicMock()
        mock_vm.vm_id = "sbx-published-1"
        mock_vm.info.status = VMState.RUNNING
        mock_vm.info.config.backend = expected_backend
        mock_vm.info.network = MagicMock(spec=NetworkConfig)
        mock_vm.info.network.guest_ip = "172.16.0.2"
        mock_vm.info.network.ssh_host_port = 2200
        mock_vm_cls.return_value = mock_vm

        # If apply_preset gets called, this test should fail loudly.
        with patch("smolvm.presets.apply_preset") as mock_apply:
            ret = main(["openclaw", "start", "--json"])

            mock_apply.assert_not_called()

        assert ret == 0
        mock_ensure_image.assert_called_once_with("openclaw", expected_arch, expected_vmm)

        # Verify VMConfig was built with the right wiring.
        config_arg = mock_vm_cls.call_args[0][0]
        assert config_arg.kernel_path == kernel
        assert config_arg.rootfs_path == rootfs
        assert config_arg.backend == expected_backend
        assert config_arg.ssh_public_key == "ssh-ed25519 AAAAExampleKey user@host"
        assert "init=/init" in config_arg.boot_args
        if expected_vmm == "qemu":
            expected_console = "ttyAMA0" if expected_arch == "arm64" else "ttyS0"
            assert f"console={expected_console}" in config_arg.boot_args

        # Success path: VM is left running so the user can ssh in. stop()
        # and delete() must NOT have been called — only close() to release
        # SDK handles.
        mock_vm.stop.assert_not_called()
        mock_vm.delete.assert_not_called()
        mock_vm.close.assert_called_once()

    @patch("smolvm.cli.main.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.images.published.ensure_published_image")
    @patch("smolvm.cli.main.platform.machine", return_value="arm64")
    @patch("smolvm.cli.main.platform.system", return_value="Darwin")
    def test_published_path_reaps_vm_on_failure(
        self,
        _mock_system: MagicMock,
        _mock_machine: MagicMock,
        mock_ensure_image: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_vm_cls: MagicMock,
        _mock_subprocess: MagicMock,
        tmp_path: Path,
    ) -> None:
        """If wait_for_ssh fails, the VM (and its QEMU process) must be
        stopped and deleted — not just close()d, which only releases SDK
        handles and leaves the runtime burning CPU."""
        from smolvm.exceptions import OperationTimeoutError
        from smolvm.images.manager import LocalImage

        kernel = tmp_path / "vmlinux.bin"
        rootfs = tmp_path / "rootfs.ext4"
        priv = tmp_path / "id_ed25519"
        pub = tmp_path / "id_ed25519.pub"
        for p in (kernel, rootfs, priv):
            p.touch()
        pub.write_text("ssh-ed25519 AAAAExampleKey user@host\n")

        mock_ensure_image.return_value = LocalImage(
            name="openclaw-v0.0.13-arm64-qemu",
            kernel_path=kernel,
            rootfs_path=rootfs,
        )
        mock_ensure_ssh_key.return_value = (priv, pub)
        mock_vm = MagicMock()
        mock_vm.vm_id = "sbx-published-leak"
        mock_vm.wait_for_ssh.side_effect = OperationTimeoutError(
            "wait_for_ssh: simulated timeout", 30.0
        )
        mock_vm_cls.return_value = mock_vm

        ret = main(["openclaw", "start", "--json"])

        assert ret == 1  # OperationTimeoutError → exit 1
        mock_vm.start.assert_called_once()
        mock_vm.stop.assert_called_once()
        mock_vm.delete.assert_called_once()
        mock_vm.close.assert_called_once()
