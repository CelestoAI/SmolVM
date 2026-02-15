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
from smolvm.types import VMState


class TestCliEnv:
    """Tests for `smolvm env` subcommands."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        # Patch where the class is defined, not where it's imported in the function
        with patch("smolvm.vm.SmolVM") as m:
            yield m

    @pytest.fixture
    def mock_ssh_cls(self) -> MagicMock:
        with patch("smolvm.ssh.SSHClient") as m:
            yield m

    @pytest.fixture
    def mock_ensure_ssh_key(self) -> MagicMock:
        with patch("smolvm.utils.ensure_ssh_key") as m:
            m.return_value = ("/tmp/id_ed25519", "/tmp/id_ed25519.pub")
            yield m

    @pytest.fixture
    def mock_inject(self) -> MagicMock:
        with patch("smolvm.env.inject_env_vars") as m:
            yield m

    @pytest.fixture
    def mock_remove(self) -> MagicMock:
        with patch("smolvm.env.remove_env_vars") as m:
            yield m

    @pytest.fixture
    def mock_read(self) -> MagicMock:
        with patch("smolvm.env.read_env_vars") as m:
            yield m

    def _setup_vm(self, mock_sdk_cls: MagicMock, vm_id: str = "vm001") -> MagicMock:
        mock_sdk = MagicMock()
        mock_info = MagicMock(vm_id=vm_id, status=VMState.RUNNING)
        mock_info.network.guest_ip = "172.16.0.2"
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.from_id.return_value = mock_sdk
        return mock_sdk

    def test_env_set_success(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_inject: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env set` success path."""
        self._setup_vm(mock_sdk_cls)
        mock_inject.return_value = ["FOO"]

        ret = main(["env", "set", "vm001", "FOO=bar"])

        assert ret == 0
        mock_inject.assert_called_once()
        args = mock_inject.call_args
        assert args[0][1] == {"FOO": "bar"}  # env_vars dict
        assert "Set 1 env var(s)" in capsys.readouterr().out

    def test_env_set_multiple(
        self,
        mock_sdk_cls: MagicMock,
        mock_inject: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_ssh_cls: MagicMock,
    ) -> None:
        """Test `smolvm env set` with multiple variables."""
        self._setup_vm(mock_sdk_cls)
        mock_inject.return_value = ["A", "B"]

        ret = main(["env", "set", "vm001", "A=1", "B=2"])

        assert ret == 0
        mock_inject.assert_called_once()
        assert mock_inject.call_args[0][1] == {"A": "1", "B": "2"}

    def test_env_set_malformed_pair_fails(
        self, 
        mock_sdk_cls: MagicMock, 
        capsys: pytest.CaptureFixture
    ) -> None:
        """Test execution fails on malformed key=value pair."""
        self._setup_vm(mock_sdk_cls)
        
        with pytest.raises(SystemExit) as exc:
            main(["env", "set", "vm001", "BADPAIR"])
        
        # When raising SystemExit("string"), the code IS the string
        assert "malformed pair" in str(exc.value.code)


    def test_env_unset_success(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_remove: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env unset` success path."""
        self._setup_vm(mock_sdk_cls)
        mock_remove.return_value = {"FOO": "bar"}

        ret = main(["env", "unset", "vm001", "FOO"])

        assert ret == 0
        mock_remove.assert_called_once()
        assert "Removed 1 env var(s)" in capsys.readouterr().out

    def test_env_list_success(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_read: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env list` success path (masked by default)."""
        self._setup_vm(mock_sdk_cls)
        mock_read.return_value = {"FOO": "bar", "SECRET": "xyz"}

        ret = main(["env", "list", "vm001"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "FOO=****" in out
        assert "SECRET=****" in out
        assert "bar" not in out  # Values hidden

    def test_env_list_show_values(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_read: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test `smolvm env list --show-values` reveals values."""
        self._setup_vm(mock_sdk_cls)
        mock_read.return_value = {"FOO": "bar"}

        ret = main(["env", "list", "vm001", "--show-values"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "FOO=bar" in out

    def test_explicit_ssh_key_args(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_read: MagicMock,
    ) -> None:
        """Test passing explicit SSH key and user via CLI args."""
        self._setup_vm(mock_sdk_cls)
        mock_read.return_value = {}

        # Flags for 'env' must come before the subcommand 'list'
        main([
            "env",
            "--ssh-key", "/custom/key",
            "--ssh-user", "custom-user",
            "list", "vm001",
        ])

        mock_ssh_cls.assert_called_once()
        kwargs = mock_ssh_cls.call_args[1]
        assert kwargs["key_path"] == "/custom/key"
        assert kwargs["user"] == "custom-user"

    def test_vm_lookup_failure_prints_error(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test handling of VM lookup failure."""
        mock_sdk_cls.from_id.side_effect = Exception("VM not found")

        ret = main(["env", "list", "missing-vm"])

        assert ret == 1
        assert "Error: VM not found" in capsys.readouterr().out

    def test_vm_no_network_prints_error(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Test handling of VM without network config."""
        mock_sdk = self._setup_vm(mock_sdk_cls)
        mock_sdk.get.return_value.network = None

        ret = main(["env", "list", "vm001"])

        assert ret == 1
        assert "no network configuration" in capsys.readouterr().out
