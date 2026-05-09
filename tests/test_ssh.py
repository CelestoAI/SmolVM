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

import logging
import socket
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.ssh import SSHClient
from smolvm.types import CommandResult


class TestParamikoLoggerSilenced:
    """Importing smolvm.ssh must silence paramiko's transport logger.

    Rationale: during ``wait_for_ssh`` retries, paramiko's Transport thread
    logs ``Exception (client): Error reading SSH protocol banner`` at ERROR
    level when sshd is briefly unavailable. SmolVM catches the SSHException
    and retries successfully, so the stderr noise from the failed attempt is
    misleading. The fix sets the ``paramiko.transport`` logger to CRITICAL
    so per-retry races stay quiet; smolvm still surfaces real errors via
    SmolVMError with the original exception chained.
    """

    def test_paramiko_transport_logger_is_silenced_on_import(self) -> None:
        # The top-level ``from smolvm.ssh import SSHClient`` above has
        # already triggered the module-level ``setLevel`` call as a side
        # effect of import. We just verify the resulting state here.
        level = logging.getLogger("paramiko.transport").getEffectiveLevel()
        assert level >= logging.CRITICAL, (
            f"paramiko.transport logger level is {level}, expected >= CRITICAL "
            f"({logging.CRITICAL}) so retry-loop EOF noise stays silent"
        )

    def test_smolvm_ssh_logger_is_not_silenced(self) -> None:
        """Silencing paramiko.transport must not affect smolvm's own logger."""
        # smolvm.ssh.logger should remain at its default (NOTSET / inherited),
        # so smolvm's own info/debug messages are still surfaced.
        smolvm_level = logging.getLogger("smolvm.ssh").getEffectiveLevel()
        # Anything strictly below CRITICAL means we didn't accidentally
        # blanket-silence the smolvm namespace.
        assert smolvm_level < logging.CRITICAL, (
            f"smolvm.ssh logger level is {smolvm_level}; the paramiko "
            "silencer must not affect smolvm's own loggers"
        )


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


def _mock_exec_result(exit_code: int, stdout: str, stderr: str) -> tuple[None, MagicMock, MagicMock]:
    stdout_ch = MagicMock()
    stderr_ch = MagicMock()
    stdout_ch.read.return_value = stdout.encode("utf-8")
    stderr_ch.read.return_value = stderr.encode("utf-8")
    stdout_ch.channel.recv_exit_status.return_value = exit_code
    return (None, stdout_ch, stderr_ch)


class TestSSHClientWarningPolicy:
    """Tests that _connect uses WarningPolicy (not AutoAddPolicy)."""

    @patch("smolvm.ssh.paramiko.SSHClient")
    def test_connect_uses_warning_policy(self, mock_ssh_client_cls: MagicMock) -> None:
        """Verify set_missing_host_key_policy is called with WarningPolicy."""
        import paramiko

        mock_client = MagicMock()
        mock_ssh_client_cls.return_value = mock_client

        client = SSHClient("172.16.0.2")
        client._connect()

        mock_client.set_missing_host_key_policy.assert_called_once()
        policy_arg = mock_client.set_missing_host_key_policy.call_args[0][0]
        assert isinstance(policy_arg, paramiko.WarningPolicy), (
            f"Expected WarningPolicy, got {type(policy_arg).__name__}. "
            "AutoAddPolicy silently accepts any key; WarningPolicy logs the "
            "unknown key, which is safer for ephemeral VMs."
        )


class TestSSHClientRun:
    """Tests for SSH command execution."""

    @patch.object(SSHClient, "_ensure_connected")
    def test_run_success(self, mock_connected: MagicMock) -> None:
        """Test successful command execution."""
        mock_client = MagicMock()
        mock_client.exec_command.return_value = _mock_exec_result(0, "hello world\n", "")
        mock_connected.return_value = mock_client

        client = SSHClient("172.16.0.2")
        result = client.run("echo hello world")

        assert isinstance(result, CommandResult)
        assert result.exit_code == 0
        assert result.stdout == "hello world\n"
        assert result.stderr == ""
        assert result.ok is True

    @patch.object(SSHClient, "_ensure_connected")
    def test_run_nonzero_exit(self, mock_connected: MagicMock) -> None:
        """Test command with non-zero exit code."""
        mock_client = MagicMock()
        mock_client.exec_command.return_value = _mock_exec_result(1, "", "command not found\n")
        mock_connected.return_value = mock_client

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

    @patch.object(SSHClient, "_ensure_connected")
    def test_run_timeout(self, mock_connected: MagicMock) -> None:
        """Test SSH timeout raises OperationTimeoutError."""
        mock_client = MagicMock()
        mock_client.exec_command.side_effect = socket.timeout("timed out")
        mock_connected.return_value = mock_client

        client = SSHClient("172.16.0.2")
        with pytest.raises(OperationTimeoutError):
            client.run("sleep 100", timeout=5)

    @patch.object(SSHClient, "_ensure_connected")
    def test_run_connection_failure_raises(self, mock_connected: MagicMock) -> None:
        """Test connection errors raise SmolVMError."""
        mock_connected.side_effect = SmolVMError("SSH connection failed: boom")

        client = SSHClient("172.16.0.2")
        with pytest.raises(SmolVMError, match="SSH connection failed"):
            client.run("echo test")

    @patch.object(SSHClient, "_ensure_connected")
    def test_run_builds_correct_command(self, mock_connected: MagicMock) -> None:
        """Test that the SSH command is built correctly."""
        mock_client = MagicMock()
        mock_client.exec_command.return_value = _mock_exec_result(0, "", "")
        mock_connected.return_value = mock_client

        client = SSHClient("172.16.0.2", user="admin", port=2222, key_path="/tmp/key")
        client.run("uname -r")

        remote_command = mock_client.exec_command.call_args.args[0]
        timeout = mock_client.exec_command.call_args.kwargs["timeout"]

        assert remote_command.startswith('SHELL_BIN="${SHELL:-/bin/sh}"; exec "$SHELL_BIN" -lc ')
        assert "uname -r" in remote_command
        assert timeout == 30

    @patch.object(SSHClient, "_ensure_connected")
    def test_run_raw_shell_uses_unwrapped_command(self, mock_connected: MagicMock) -> None:
        """Test raw mode sends command without login-shell wrapping."""
        mock_client = MagicMock()
        mock_client.exec_command.return_value = _mock_exec_result(0, "", "")
        mock_connected.return_value = mock_client

        client = SSHClient("172.16.0.2")
        client.run("uname -r", shell="raw")

        remote_command = mock_client.exec_command.call_args.args[0]
        assert remote_command == "uname -r"


class TestSSHClientWaitForSSH:
    """Tests for SSH readiness polling."""

    @patch.object(SSHClient, "_connect")
    @patch.object(SSHClient, "_tcp_port_open", return_value=True)
    def test_wait_succeeds_immediately(self, _: MagicMock, mock_connect: MagicMock) -> None:
        """Test wait_for_ssh succeeds on first attempt."""
        mock_connect.return_value = MagicMock()

        client = SSHClient("172.16.0.2")
        client.wait_for_ssh(timeout=5)  # Should not raise

    @patch("smolvm.ssh.time.sleep", return_value=None)
    @patch.object(SSHClient, "_tcp_port_open", return_value=False)
    def test_wait_timeout_raises(self, _: MagicMock, __: MagicMock) -> None:
        """Test wait_for_ssh raises on timeout."""
        client = SSHClient("172.16.0.2")
        with pytest.raises(OperationTimeoutError):
            client.wait_for_ssh(timeout=0.2, interval=0.05)

    def test_wait_invalid_timeout_raises(self) -> None:
        """Test that invalid timeout raises ValueError."""
        client = SSHClient("172.16.0.2")
        with pytest.raises(ValueError, match="timeout must be"):
            client.wait_for_ssh(timeout=0)
