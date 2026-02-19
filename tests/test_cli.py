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

import os
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli import DASHBOARD_ALLOW_BETA_ENV, main


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

    def test_env_set_malformed_pair_fails(self, mock_vm_cls: MagicMock) -> None:
        """Test execution fails on malformed key=value pair."""
        vm = self._setup_vm(mock_vm_cls)

        with pytest.raises(SystemExit) as exc:
            main(["env", "set", "vm001", "BADPAIR"])

        assert "malformed pair" in str(exc.value.code)
        vm.close.assert_called_once()

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
        assert "FOO=****" in out
        assert "SECRET=****" in out
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
        assert "FOO=bar" in out

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
        assert "Error: VM not found" in capsys.readouterr().out

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
        assert "no network configuration" in capsys.readouterr().out
        vm.close.assert_called_once()


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


class TestCliDashboard:
    """Tests for `smolvm dashboard`."""

    @patch("smolvm.cli.importlib.import_module")
    def test_dashboard_defaults(self, mock_import: MagicMock) -> None:
        """`smolvm dashboard` should launch uvicorn with defaults."""
        mock_uvicorn = MagicMock()
        mock_import.return_value = mock_uvicorn

        ret = main(["dashboard"])

        assert ret == 0
        mock_import.assert_called_once_with("uvicorn")
        mock_uvicorn.run.assert_called_once_with(
            "smolvm.dashboard.server:app",
            host="127.0.0.1",
            port=8080,
        )

    @patch("smolvm.cli.importlib.import_module")
    def test_dashboard_custom_port(self, mock_import: MagicMock) -> None:
        """Custom host/port should be forwarded to uvicorn."""
        mock_uvicorn = MagicMock()
        mock_import.return_value = mock_uvicorn

        ret = main(["dashboard", "--host", "0.0.0.0", "--port", "9090"])

        assert ret == 0
        mock_uvicorn.run.assert_called_once_with(
            "smolvm.dashboard.server:app",
            host="0.0.0.0",
            port=9090,
        )

    @patch("smolvm.cli.importlib.import_module")
    def test_dashboard_command_alias(self, mock_import: MagicMock) -> None:
        """Top-level `smolvm dashboard` should behave like `start dashboard`."""
        mock_uvicorn = MagicMock()
        mock_import.return_value = mock_uvicorn

        ret = main(["dashboard", "--port", "8181"])

        assert ret == 0
        mock_uvicorn.run.assert_called_once_with(
            "smolvm.dashboard.server:app",
            host="127.0.0.1",
            port=8181,
        )

    @patch("smolvm.cli.importlib.import_module")
    def test_start_dashboard_allow_beta_sets_env(self, mock_import: MagicMock) -> None:
        """--allow-beta should set env flag while uvicorn starts."""
        mock_uvicorn = MagicMock()

        def _run(*args: object, **kwargs: object) -> None:
            assert os.environ.get(DASHBOARD_ALLOW_BETA_ENV) == "1"

        mock_uvicorn.run.side_effect = _run
        mock_import.return_value = mock_uvicorn

        os.environ.pop(DASHBOARD_ALLOW_BETA_ENV, None)
        ret = main(["start", "dashboard", "--allow-beta"])

        assert ret == 0
        assert DASHBOARD_ALLOW_BETA_ENV not in os.environ

    @patch("smolvm.cli.importlib.import_module", side_effect=ImportError)
    def test_dashboard_missing_dependency(
        self,
        _: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Missing dashboard extras should return an actionable error."""
        ret = main(["dashboard"])

        assert ret == 1
        assert "smolvm[dashboard]" in capsys.readouterr().out

    @patch("smolvm.cli.importlib.import_module")
    def test_dashboard_invalid_port(
        self,
        mock_import: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Out-of-range ports should fail fast with usage error code."""
        mock_import.return_value = MagicMock()

        ret = main(["dashboard", "--port", "70000"])

        assert ret == 2
        assert "invalid port" in capsys.readouterr().out
