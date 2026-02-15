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

"""Tests for SmolVM CLI environment commands."""

from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli import main


class TestCliEnv:
    """Tests for `smolvm env` subcommands."""

    @pytest.fixture
    def mock_vm_cls(self) -> MagicMock:
        with patch("smolvm.facade.VM") as m:
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

        main([
            "env",
            "list",
            "vm001",
            "--ssh-key",
            "/custom/key",
            "--ssh-user",
            "custom-user",
        ])

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
