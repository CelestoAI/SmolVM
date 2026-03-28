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
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli import DASHBOARD_ALLOW_BETA_ENV, _current_version_is_prerelease, build_parser, main
from smolvm.types import NetworkConfig, VMState


def _make_vm_info(
    vm_id: str = "vm-abc123",
    status: VMState = VMState.RUNNING,
    guest_ip: str = "172.16.0.2",
    ssh_host_port: int | None = 2200,
    pid: int | None = 12345,
) -> MagicMock:
    """Build a lightweight VMInfo-like mock for list tests."""
    vm = MagicMock()
    vm.vm_id = vm_id
    vm.status = status
    vm.pid = pid
    if guest_ip:
        vm.network = MagicMock(spec=NetworkConfig)
        vm.network.guest_ip = guest_ip
        vm.network.ssh_host_port = ssh_host_port
    else:
        vm.network = None
    return vm


def test_top_level_help_mentions_json_for_agents() -> None:
    """Top-level help should describe the machine-readable JSON mode."""
    help_text = build_parser().format_help()

    assert "--json" in help_text
    assert "machine-readable output" in help_text
    assert "LLMs, agents, and automation" in help_text


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


class TestCliCreate:
    """Tests for `smolvm create`."""

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_create_success(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm create` should build, start, and report a named VM."""
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
                "--memory-mib",
                "1024",
                "--disk-size-mib",
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
            backend="qemu",
            mem_size_mib=1024,
            disk_size_mib=2048,
            ssh_key_path=None,
        )
        mock_vm_cls.assert_called_once_with(config, ssh_key_path="/tmp/id_ed25519")
        vm.start.assert_called_once_with(boot_timeout=45.0)
        vm.wait_for_ssh.assert_called_once_with(timeout=45.0)
        vm.stop.assert_not_called()
        vm.delete.assert_not_called()
        vm.close.assert_called_once()
        out = capsys.readouterr().out
        assert "Created VM 'project-spacex'." in out
        assert "Backend" in out
        assert "qemu" in out
        assert "172.16.0.2" in out
        assert "2200" in out
        assert "smolvm ssh project-spacex" in out

    @patch("smolvm.facade._build_auto_config")
    @patch("smolvm.facade.SmolVM")
    def test_create_json(
        self,
        mock_vm_cls: MagicMock,
        mock_build_auto_config: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`smolvm create --json` should emit the shared envelope."""
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
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "create"
        assert payload["data"]["vm"]["name"] == "project-spacex"
        assert payload["data"]["vm"]["backend"] == "qemu"
        assert payload["data"]["next"]["ssh_command"] == "smolvm ssh project-spacex"

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


class TestCliSSH:
    """Tests for `smolvm ssh`."""

    @patch("smolvm.cli.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_running_vm_launches_subprocess(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """`smolvm ssh` should attach to a running VM without restarting it."""
        vm = MagicMock()
        vm.status = VMState.RUNNING
        vm._ssh_attach_command.return_value = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            "2200",
            "-i",
            "/custom/key",
            "-o",
            "IdentitiesOnly=yes",
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
        vm.wait_for_ssh.assert_called_once_with(timeout=15.0)
        vm._ssh_attach_command.assert_called_once_with()
        mock_run.assert_called_once_with(vm._ssh_attach_command.return_value, check=False)
        vm.close.assert_called_once()

    @pytest.mark.parametrize("status", [VMState.CREATED, VMState.STOPPED])
    @patch("smolvm.cli.subprocess.run")
    @patch("smolvm.facade.SmolVM")
    def test_ssh_auto_starts_created_or_stopped_vm(
        self,
        mock_vm_cls: MagicMock,
        mock_run: MagicMock,
        status: VMState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`smolvm ssh` should auto-start attachable non-running VMs."""
        vm = MagicMock()
        vm.status = status
        vm._ssh_attach_command.return_value = ["ssh", "root@127.0.0.1"]
        mock_vm_cls.from_id.return_value = vm
        mock_run.return_value = MagicMock(returncode=0)

        ret = main(["ssh", "vm001"])

        assert ret == 0
        out = capsys.readouterr().out
        assert (
            "Notice: VM 'vm001' isn't running yet. SSH may take a little longer while SmolVM starts it."
            in out
        )
        vm.start.assert_called_once_with(boot_timeout=30.0)
        vm.wait_for_ssh.assert_called_once_with(timeout=30.0)
        mock_run.assert_called_once_with(["ssh", "root@127.0.0.1"], check=False)

    @patch("smolvm.cli.subprocess.run")
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

    @patch("smolvm.cli.subprocess.run")
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

    @patch("smolvm.cli.subprocess.run", side_effect=FileNotFoundError)
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

    @patch("smolvm.cli.subprocess.run")
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

    @patch("smolvm.cli.run_doctor")
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

    @patch("smolvm.cli.run_doctor")
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


class TestCurrentVersionIsPrerelease:
    """Tests for _current_version_is_prerelease helper."""

    @patch("smolvm.cli.importlib.metadata.version", return_value="0.0.5.a1")
    def test_alpha_version_is_prerelease(self, _: MagicMock) -> None:
        """Alpha versions (e.g. 0.0.5.a1) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.importlib.metadata.version", return_value="0.0.5.dev1")
    def test_dev_version_is_prerelease(self, _: MagicMock) -> None:
        """Dev versions (e.g. 0.0.5.dev1) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.importlib.metadata.version", return_value="0.0.5b2")
    def test_beta_version_is_prerelease(self, _: MagicMock) -> None:
        """Beta versions (e.g. 0.0.5b2) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.importlib.metadata.version", return_value="0.0.5rc1")
    def test_rc_version_is_prerelease(self, _: MagicMock) -> None:
        """Release candidates (e.g. 0.0.5rc1) should be detected as pre-release."""
        assert _current_version_is_prerelease() is True

    @patch("smolvm.cli.importlib.metadata.version", return_value="0.0.5")
    def test_stable_version_is_not_prerelease(self, _: MagicMock) -> None:
        """Stable versions (e.g. 0.0.5) should NOT be detected as pre-release."""
        assert _current_version_is_prerelease() is False

    @patch("smolvm.cli.importlib.metadata.version", return_value="1.2.3")
    def test_stable_semver_is_not_prerelease(self, _: MagicMock) -> None:
        """Stable semantic versions (e.g. 1.2.3) should NOT be detected as pre-release."""
        assert _current_version_is_prerelease() is False

    def test_package_not_found_returns_false(self) -> None:
        """PackageNotFoundError should be handled gracefully by returning False."""
        import importlib.metadata

        with patch(
            "smolvm.cli.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("smolvm"),
        ):
            assert _current_version_is_prerelease() is False


class TestCliUi:
    """Tests for `smolvm ui`."""

    @patch("smolvm.cli.importlib.import_module")
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

    @patch("smolvm.cli.importlib.import_module")
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

    @patch("smolvm.cli.importlib.import_module")
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

    @patch("smolvm.cli.importlib.import_module", side_effect=ImportError)
    def test_ui_missing_dependency(
        self,
        _: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Missing dashboard extras should return an actionable error."""
        ret = main(["ui"])

        assert ret == 1
        assert "smolvm[dashboard]" in capsys.readouterr().err

    @patch("smolvm.cli.importlib.import_module")
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

    @patch("smolvm.cli.importlib.import_module")
    def test_ui_auto_beta_for_prerelease_version(
        self,
        mock_import: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Pre-release smolvm version should auto-enable beta UI assets."""
        monkeypatch.setattr("smolvm.cli._current_version_is_prerelease", lambda: True)
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

    @patch("smolvm.cli.importlib.import_module")
    def test_ui_no_auto_beta_for_stable_version(
        self,
        mock_import: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Stable smolvm version should NOT auto-enable beta UI assets."""
        monkeypatch.setattr("smolvm.cli._current_version_is_prerelease", lambda: False)
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
