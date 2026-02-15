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

"""Tests for SmolVM SSH module."""

from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.ssh import SSHClient
from smolvm.types import CommandResult


class TestSSHClientInit:
    """Tests for SSHClient initialization."""

    def test_valid_init(self) -> None:
        """Test valid initialization."""
        client = SSHClient("172.16.0.2")
        assert client.host == "172.16.0.2"
        assert client.user == "root"
        assert client.port == 22

    def test_custom_params(self) -> None:
        """Test initialization with custom params."""
        client = SSHClient(
            "10.0.0.1",
            user="admin",
            port=2222,
            key_path="/tmp/id_rsa",
            connect_timeout=30,
        )
        assert client.host == "10.0.0.1"
        assert client.user == "admin"
        assert client.port == 2222
        assert client.key_path == "/tmp/id_rsa"

    def test_empty_host_raises(self) -> None:
        """Test that empty host raises ValueError."""
        with pytest.raises(ValueError, match="host cannot be empty"):
            SSHClient("")

    def test_empty_user_raises(self) -> None:
        """Test that empty user raises ValueError."""
        with pytest.raises(ValueError, match="user cannot be empty"):
            SSHClient("172.16.0.2", user="")

    def test_invalid_port_raises(self) -> None:
        """Test that invalid port raises ValueError."""
        with pytest.raises(ValueError, match="port must be"):
            SSHClient("172.16.0.2", port=0)
        with pytest.raises(ValueError, match="port must be"):
            SSHClient("172.16.0.2", port=70000)


class TestSSHClientRun:
    """Tests for SSH command execution."""

    @patch("smolvm.ssh.subprocess.run")
    def test_run_success(self, mock_run: MagicMock) -> None:
        """Test successful command execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="hello world\n",
            stderr="",
        )

        client = SSHClient("172.16.0.2")
        result = client.run("echo hello world")

        assert isinstance(result, CommandResult)
        assert result.exit_code == 0
        assert result.stdout == "hello world\n"
        assert result.stderr == ""
        assert result.ok is True

    @patch("smolvm.ssh.subprocess.run")
    def test_run_nonzero_exit(self, mock_run: MagicMock) -> None:
        """Test command with non-zero exit code."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="command not found\n",
        )

        client = SSHClient("172.16.0.2")
        result = client.run("bad-command")

        assert result.exit_code == 1
        assert result.ok is False
        assert "command not found" in result.stderr

    def test_run_empty_command_raises(self) -> None:
        """Test that empty command raises ValueError."""
        client = SSHClient("172.16.0.2")
        with pytest.raises(ValueError, match="command cannot be empty"):
            client.run("")

    def test_run_whitespace_command_raises(self) -> None:
        """Test that whitespace-only command raises ValueError."""
        client = SSHClient("172.16.0.2")
        with pytest.raises(ValueError, match="command cannot be empty"):
            client.run("   ")

    def test_run_invalid_shell_mode_raises(self) -> None:
        """Test invalid shell mode raises ValueError."""
        client = SSHClient("172.16.0.2")
        with pytest.raises(ValueError, match="shell must be"):
            client.run("echo ok", shell="interactive")  # type: ignore[arg-type]

    @patch("smolvm.ssh.subprocess.run")
    def test_run_timeout(self, mock_run: MagicMock) -> None:
        """Test SSH timeout raises OperationTimeoutError."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=5)

        client = SSHClient("172.16.0.2")
        with pytest.raises(OperationTimeoutError):
            client.run("sleep 100", timeout=5)

    @patch("smolvm.ssh.subprocess.run")
    def test_run_ssh_not_found(self, mock_run: MagicMock) -> None:
        """Test missing ssh binary raises SmolVMError."""
        mock_run.side_effect = FileNotFoundError()

        client = SSHClient("172.16.0.2")
        with pytest.raises(SmolVMError, match="ssh binary not found"):
            client.run("echo test")

    @patch("smolvm.ssh.subprocess.run")
    def test_run_builds_correct_command(self, mock_run: MagicMock) -> None:
        """Test that the SSH command is built correctly."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        client = SSHClient("172.16.0.2", user="admin", port=2222, key_path="/tmp/key")
        client.run("uname -r")

        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "ssh"
        assert "-p" in call_args
        assert "2222" in call_args
        assert "-i" in call_args
        assert "/tmp/key" in call_args
        assert "admin@172.16.0.2" in call_args
        assert call_args[-1].startswith("SHELL_BIN=")
        assert "uname -r" in call_args[-1]

    @patch("smolvm.ssh.subprocess.run")
    def test_run_raw_shell_uses_unwrapped_command(self, mock_run: MagicMock) -> None:
        """Test raw mode sends command without login-shell wrapping."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        client = SSHClient("172.16.0.2")
        client.run("uname -r", shell="raw")

        call_args = mock_run.call_args[0][0]
        assert call_args[-1] == "uname -r"


class TestSSHClientWaitForSSH:
    """Tests for SSH readiness polling."""

    @patch("smolvm.ssh.subprocess.run")
    def test_wait_succeeds_immediately(self, mock_run: MagicMock) -> None:
        """Test wait_for_ssh succeeds on first attempt."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="__smolvm_ready__\n",
            stderr="",
        )

        client = SSHClient("172.16.0.2")
        client.wait_for_ssh(timeout=5)  # Should not raise

    @patch("smolvm.ssh.subprocess.run")
    def test_wait_timeout_raises(self, mock_run: MagicMock) -> None:
        """Test wait_for_ssh raises on timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=5)

        client = SSHClient("172.16.0.2")
        with pytest.raises(OperationTimeoutError):
            client.wait_for_ssh(timeout=0.5, interval=0.1)

    def test_wait_invalid_timeout_raises(self) -> None:
        """Test that invalid timeout raises ValueError."""
        client = SSHClient("172.16.0.2")
        with pytest.raises(ValueError, match="timeout must be"):
            client.wait_for_ssh(timeout=0)
