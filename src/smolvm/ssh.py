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

"""SSH command execution for SmolVM guests.

Uses the system ``ssh`` binary to execute commands on guest VMs via their
allocated IP addresses.  This avoids a heavy dependency on paramiko while
leveraging the SSH binary that is already a checked dependency in
:class:`~smolvm.host.HostManager`.
"""

import logging
import shlex
import subprocess
import time
from typing import Literal

from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.types import CommandResult

logger = logging.getLogger(__name__)

# Default SSH options for local microVM connections.
# StrictHostKeyChecking=no is acceptable here because VMs run on a
# private host-only subnet (172.16.0.0/24) and are ephemeral.
_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "BatchMode=yes",
]

ShellMode = Literal["login", "raw"]


class SSHClient:
    """Execute commands on a microVM guest via SSH.

    Args:
        host: Guest IP address.
        user: SSH user (default ``root``).
        port: SSH port on the guest (default ``22``).
        key_path: Optional path to an SSH private key file.
        connect_timeout: Seconds to wait for the TCP connection.
    """

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: str | None = None,
        connect_timeout: int = 10,
    ) -> None:
        if not host:
            raise ValueError("host cannot be empty")
        if not user:
            raise ValueError("user cannot be empty")
        if port < 1 or port > 65535:
            raise ValueError(f"port must be 1-65535, got {port}")
        if connect_timeout < 1:
            raise ValueError("connect_timeout must be >= 1")

        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.connect_timeout = connect_timeout

    def _build_ssh_cmd(self, command: str) -> list[str]:
        """Build the ssh command line.

        Args:
            command: Command to run on the guest.

        Returns:
            List of command-line arguments.
        """
        cmd = [
            "ssh",
            *_SSH_OPTS,
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-p",
            str(self.port),
        ]

        if self.key_path:
            cmd.extend(["-i", self.key_path])

        cmd.append(f"{self.user}@{self.host}")
        cmd.append(command)
        return cmd

    @staticmethod
    def _wrap_login_shell_command(command: str) -> str:
        """Wrap a command so it runs inside a login shell."""
        quoted_command = shlex.quote(command)
        return (
            "SHELL_BIN=\"${SHELL:-/bin/sh}\"; "
            "exec \"$SHELL_BIN\" -lc "
            f"{quoted_command}"
        )

    def _prepare_remote_command(self, command: str, shell: ShellMode) -> str:
        """Prepare a command string based on desired shell mode."""
        if shell == "raw":
            return command
        if shell == "login":
            return self._wrap_login_shell_command(command)
        raise ValueError("shell must be 'login' or 'raw'")

    def run(
        self,
        command: str,
        timeout: int = 30,
        shell: ShellMode = "login",
    ) -> CommandResult:
        """Execute a command on the guest VM.

        Args:
            command: Shell command to execute.
            timeout: Maximum seconds to wait for the command.
            shell: Command execution mode:
                - ``"login"`` (default): run via guest login shell.
                - ``"raw"``: execute command directly with no shell wrapping.

        Returns:
            :class:`~smolvm.types.CommandResult` with exit code, stdout,
            and stderr.

        Raises:
            ValueError: If *command* is empty.
            OperationTimeoutError: If the command exceeds *timeout*.
            SmolVMError: If the SSH process cannot be started.
        """
        if not command or not command.strip():
            raise ValueError("command cannot be empty")
        if timeout < 1:
            raise ValueError("timeout must be >= 1")

        remote_command = self._prepare_remote_command(command, shell=shell)
        cmd = self._build_ssh_cmd(remote_command)
        logger.debug(
            "SSH exec on %s:%d (shell=%s): %s",
            self.host,
            self.port,
            shell,
            command,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return CommandResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired as e:
            raise OperationTimeoutError(
                f"ssh {self.host}:{self.port}: {command}",
                timeout,
            ) from e
        except FileNotFoundError:
            raise SmolVMError("ssh binary not found. Install openssh-client.") from None
        except OSError as e:
            raise SmolVMError(f"Failed to execute SSH: {e}") from e

    def wait_for_ssh(self, timeout: float = 60.0, interval: float = 2.0) -> None:
        """Wait for the SSH daemon to become reachable on the guest.

        Polls until a connection succeeds or *timeout* is exceeded.

        Args:
            timeout: Maximum seconds to wait.
            interval: Seconds between connection attempts.

        Raises:
            OperationTimeoutError: If SSH does not become available
                within *timeout* seconds.
        """
        if timeout <= 0:
            raise ValueError("timeout must be > 0")

        deadline = time.monotonic() + timeout
        last_error: str = ""

        logger.info(
            "Waiting for SSH on %s:%d (timeout=%.0fs)",
            self.host,
            self.port,
            timeout,
        )

        while time.monotonic() < deadline:
            try:
                result = self.run("echo __smolvm_ready__", timeout=5, shell="raw")
                if "__smolvm_ready__" in result.stdout:
                    logger.info("SSH is ready on %s:%d", self.host, self.port)
                    return
            except (SmolVMError, OperationTimeoutError) as e:
                last_error = str(e)
                logger.debug(
                    "SSH not ready yet on %s:%d: %s",
                    self.host,
                    self.port,
                    last_error,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise OperationTimeoutError(
            f"wait_for_ssh({self.host}:{self.port}): last error: {last_error}",
            timeout,
        )
