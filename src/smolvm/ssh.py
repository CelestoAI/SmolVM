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

Uses `paramiko <https://www.paramiko.org/>`_ for persistent SSH connections
to guest VMs.  A single TCP connection is established on first use (or during
:meth:`SSHClient.wait_for_ssh`) and reused for all subsequent commands,
eliminating the ~170ms overhead of forking a new ``ssh`` process per call.
"""

import base64
import logging
import math
import shlex
import socket
import stat
import time
import warnings
from contextlib import suppress
from pathlib import Path
from typing import Literal

import paramiko

from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.types import CommandResult

logger = logging.getLogger(__name__)

# Silence paramiko's Transport thread logger.
#
# During ``SSHClient.wait_for_ssh`` we poll a freshly-booted guest until
# sshd responds. The first connect attempts often race against cloud-init
# (which restarts sshd part-way through boot), so paramiko reads EOF on the
# banner, logs ``Exception (client): Error reading SSH protocol banner`` to
# its own logger at ERROR level from the Transport thread, *and then* raises
# SSHException to the caller. SmolVM catches that exception, retries, and
# the next attempt succeeds — but the stderr noise from the failed attempt
# is misleading because the operation as a whole succeeded.
#
# Every paramiko exception that smolvm cares about is already wrapped into
# a SmolVMError with the original message preserved (see :meth:`_connect`),
# and the original exception is chained via ``raise ... from e`` so the full
# traceback stays available. Suppressing paramiko's own transport logger to
# CRITICAL therefore loses no information that callers can't already get
# from the wrapped error — it only silences the per-retry noise.
#
# To re-enable paramiko's own logging for debugging, set the level back
# explicitly in your application code::
#
#     import logging
#     logging.getLogger("paramiko.transport").setLevel(logging.WARNING)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

# Poll backoff for wait_for_ssh: start tight (20ms) so we notice sshd within
# tens of ms of it coming up early in boot, grow ×1.5 per miss, and let the
# caller's ``interval`` cap it. The previous fixed 200ms cadence cost up to
# ~280ms of slack on a VM that booted fast.
_WAIT_BACKOFF_START = 0.02
_WAIT_BACKOFF_FACTOR = 1.5

ShellMode = Literal["login", "raw"]
ShellKind = Literal["sh", "powershell", "cmd"]
"""Guest-side login-shell wrap. ``sh`` is the POSIX default
(``$SHELL -lc <quoted>``); ``powershell`` wraps for Windows OpenSSH using
``powershell.exe -NoProfile -EncodedCommand``; ``cmd`` uses ``cmd.exe /c``.
The ``shell="raw"`` mode on :meth:`SSHClient.run` bypasses the wrap
entirely for any kind."""


def _pwsh_encoded_command(command: str) -> str:
    """Return a ``powershell.exe -NoProfile -EncodedCommand <b64>`` wrap.

    The ``-EncodedCommand`` option takes a base64-encoded UTF-16LE
    PowerShell script and sidesteps both ``cmd.exe`` and ``powershell.exe``
    quoting entirely — the command bytes survive intact regardless of what
    Windows OpenSSH does with the SSH exec request (it forwards through
    cmd.exe by default, which would otherwise mangle backtick-escaped
    quotes). This is the documented Microsoft recipe for "pass arbitrary
    PowerShell over a wire transport."

    See: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe
    """
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -EncodedCommand {encoded}"


def _cmd_wrap(command: str) -> str:
    """Wrap *command* for ``cmd.exe /s /c "..."``.

    ``/s`` makes cmd's quote-handling deterministic: with ``cmd /s /c
    "<...>"``, the *outer* double quotes are stripped and everything
    between them is passed through to the command processor. We escape
    embedded ``"`` by doubling them (cmd's in-quote escape). Shell
    metacharacters (``& | < > ^ ( )``) need no extra escaping because
    they are not interpreted inside double-quoted strings.

    Reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd
    """
    return f'cmd.exe /s /c "{command.replace(chr(34), chr(34) * 2)}"'


class SSHClient:
    """Execute commands on a microVM guest via SSH.

    Maintains a persistent paramiko SSH connection that is established
    lazily on first use and reused for all subsequent commands.

    Args:
        host: Guest IP address or hostname.
        user: SSH user (default ``root``).
        port: SSH port on the guest (default ``22``).
        key_path: Optional path to an SSH private key file.
        password: Optional password for authentication.
        connect_timeout: Seconds to wait for the TCP connection.
        shell_kind: Login-shell flavor for the guest. ``sh`` (default) wraps
            with ``$SHELL -lc``; ``powershell`` wraps with
            ``powershell.exe -NoProfile -Command`` (Windows guests);
            ``cmd`` wraps with ``cmd.exe /c``. Only consulted when
            :meth:`run` is called with ``shell="login"`` (the default);
            ``shell="raw"`` bypasses the wrap entirely.
    """

    #: Transport tag for the :class:`~smolvm.comm.base.CommChannel` interface.
    kind: str = "ssh"

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: str | None = None,
        password: str | None = None,
        connect_timeout: int = 10,
        shell_kind: ShellKind = "sh",
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
        self.password = password
        self.connect_timeout = connect_timeout
        self.shell_kind: ShellKind = shell_kind

        self._client: paramiko.SSHClient | None = None

    # ── Connection lifecycle ────────────────────────────────────

    def _connect(self) -> paramiko.SSHClient:
        """Establish a paramiko SSH connection.

        Returns:
            Connected paramiko SSHClient.

        Raises:
            SmolVMError: If connection fails.
        """
        client = paramiko.SSHClient()
        # Warn on unknown host keys instead of silently accepting them.
        # AutoAddPolicy accepts any key, making connections vulnerable to MITM
        # attacks.  WarningPolicy logs the unknown key and still connects — a
        # safe middle ground for SmolVM's ephemeral VMs whose host keys are
        # generated at boot and not yet in a managed known_hosts file.
        # TODO: pin host keys via RejectPolicy + known_hosts once the VM
        # creation pipeline exports the generated host key to the host.
        client.set_missing_host_key_policy(paramiko.WarningPolicy())  # noqa: S507

        connect_kwargs: dict[str, object] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": float(self.connect_timeout),
            "allow_agent": False,
            "look_for_keys": False,
            "banner_timeout": float(self.connect_timeout),
        }

        if self.key_path:
            connect_kwargs["key_filename"] = self.key_path
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            # Fall back to trying the agent / default keys
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = True

        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Unknown .* host key for .*",
                )
                client.connect(**connect_kwargs)  # type: ignore[arg-type]
        except Exception as e:
            client.close()
            raise SmolVMError(f"SSH connection failed: {e}") from e

        return client

    def _ensure_connected(self) -> paramiko.SSHClient:
        """Return existing connection, or create a new one.

        Automatically reconnects if the previous connection is dead.
        """
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            # Transport is dead — close and reconnect.
            logger.debug("SSH transport is dead, reconnecting to %s:%d", self.host, self.port)
            self._client.close()
            self._client = None

        self._client = self._connect()
        return self._client

    @property
    def connected(self) -> bool:
        """Check if the SSH connection is alive."""
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def close(self) -> None:
        """Close the SSH connection and release resources."""
        if self._client is not None:
            with suppress(Exception):
                self._client.close()
            self._client = None

    def get_file(self, remote_path: str, local_path: str | Path) -> Path:
        """Download a file from the guest VM using SFTP."""
        if not remote_path:
            raise ValueError("remote_path cannot be empty")

        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        client = self._ensure_connected()
        sftp = client.open_sftp()
        try:
            sftp.get(remote_path, str(destination))
        except Exception as e:
            raise SmolVMError(f"Failed to download guest file '{remote_path}': {e}") from e
        finally:
            sftp.close()

        return destination

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        """Upload a file into the guest VM using SFTP.

        When *remote_path* is an existing directory, the source filename is
        appended so the file lands inside it (matching ``cp file dir/``).
        """
        if not remote_path:
            raise ValueError("remote_path cannot be empty")

        source = Path(local_path)
        if not source.exists():
            raise ValueError(f"local_path does not exist: {source}")

        client = self._ensure_connected()
        sftp = client.open_sftp()
        try:
            # If the destination is an existing directory, append the source
            # filename so the upload lands *inside* it — matching how `cp file
            # dir/` behaves. SFTP's put() would otherwise try to overwrite the
            # directory with a regular file, which the server rejects with an
            # opaque "Failure". The stat reuses this already-open SFTP channel
            # (one extra protocol message, no new channel or guest process).
            try:
                attrs = sftp.stat(remote_path)
            except FileNotFoundError:
                pass  # Destination doesn't exist yet — treat it as a file path.
            else:
                if attrs.st_mode is not None and stat.S_ISDIR(attrs.st_mode):
                    remote_path = remote_path.rstrip("/") + "/" + source.name
            sftp.put(str(source), remote_path)
        except Exception as e:
            raise SmolVMError(f"Failed to upload file to guest '{remote_path}': {e}") from e
        finally:
            sftp.close()

    # ── Command execution ───────────────────────────────────────

    def _wrap_login_shell_command(self, command: str) -> str:
        """Wrap a command so it runs inside the guest's login shell.

        Dispatches on :attr:`shell_kind`. The POSIX ``sh`` form preserves
        the legacy ``$SHELL -lc <quoted>`` shape byte-for-byte; the
        ``powershell`` form base64-encodes the command via
        ``-EncodedCommand`` so it survives cmd.exe + PowerShell quoting
        (Windows OpenSSH routes through cmd.exe by default); ``cmd``
        uses ``cmd.exe /s /c "<doubled-quoted>"`` so embedded ``"`` and
        shell metacharacters are safe.
        """
        if self.shell_kind == "powershell":
            return _pwsh_encoded_command(command)
        if self.shell_kind == "cmd":
            return _cmd_wrap(command)
        # POSIX sh — legacy default, unchanged.
        quoted_command = shlex.quote(command)
        return f'SHELL_BIN="${{SHELL:-/bin/sh}}"; exec "$SHELL_BIN" -lc {quoted_command}'

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

        Uses the persistent SSH connection.  If the connection is not
        yet established or has been lost, it is (re)created transparently.

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
            SmolVMError: If the SSH connection cannot be established.
        """
        if not command or not command.strip():
            raise ValueError("command cannot be empty")
        if timeout < 1:
            raise ValueError("timeout must be >= 1")

        remote_command = self._prepare_remote_command(command, shell=shell)

        logger.debug(
            "SSH exec on %s:%d (shell=%s): %s",
            self.host,
            self.port,
            shell,
            command,
        )

        client = self._ensure_connected()

        try:
            _, stdout_ch, stderr_ch = client.exec_command(remote_command, timeout=timeout)
            # Read all output
            stdout = stdout_ch.read().decode("utf-8", errors="replace")
            stderr = stderr_ch.read().decode("utf-8", errors="replace")
            exit_code = stdout_ch.channel.recv_exit_status()

            return CommandResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        except TimeoutError as e:
            raise OperationTimeoutError(
                f"ssh {self.host}:{self.port}: {command}",
                timeout,
            ) from e
        except paramiko.SSHException as e:
            # Connection may have died mid-command — invalidate it
            self.close()
            raise SmolVMError(f"SSH command failed: {e}") from e
        except Exception as e:
            raise SmolVMError(f"SSH command failed: {e}") from e

    def sync(self, timeout: float = 10) -> None:
        """Flush guest filesystem buffers through the SSH control channel."""
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        result = self.run("sync", timeout=math.ceil(timeout), shell="raw")
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise SmolVMError(f"guest sync failed over SSH: {detail}")

    # ── Readiness detection ─────────────────────────────────────

    def _tcp_port_open(self, timeout: float = 0.1) -> bool:
        """Check if the SSH port is accepting TCP connections.

        This is much faster than a full SSH handshake (~1ms vs ~100ms)
        and is used to efficiently poll for port readiness before attempting
        a paramiko connect.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
                # Read the SSH banner to confirm sshd is actually ready,
                # not just TCP backlog accepting connections.
                sock.settimeout(timeout)
                data = sock.recv(32)
                return data.startswith(b"SSH-")
        except (OSError, TimeoutError):
            return False

    def wait_for_ssh(self, timeout: float = 60.0, interval: float = 0.1) -> None:
        """Wait for the SSH daemon to become reachable on the guest.

        Uses a two-phase approach for fast detection:

        1. **TCP probe** — lightweight ``socket.connect()`` calls (~1ms each)
           with a short fixed polling interval to detect when sshd is
           listening and sending its banner.
        2. **Paramiko connect** — full SSH handshake + auth.  The resulting
           connection is kept open for subsequent :meth:`run` calls.

        Both phases poll with exponential backoff (starting at
        :data:`_WAIT_BACKOFF_START`, growing ×1.5, capped at *interval*) so we
        catch sshd within tens of ms of it coming up early in boot — the common
        case — while a genuinely slow guest relaxes back to the *interval*
        cadence and is no worse off than a fixed poll.

        Args:
            timeout: Maximum seconds to wait.
            interval: Upper bound (seconds) on the poll backoff. Defaults to
                0.1s, matching the previous fixed cadence.

        Raises:
            OperationTimeoutError: If SSH does not become available
                within *timeout* seconds.
        """
        if timeout <= 0:
            raise ValueError("timeout must be > 0")

        deadline = time.monotonic() + timeout

        logger.info(
            "Waiting for SSH on %s:%d (timeout=%.0fs)",
            self.host,
            self.port,
            timeout,
        )

        # Phase 1: Fast TCP probe — detect when sshd port is open.
        backoff = _WAIT_BACKOFF_START
        while time.monotonic() < deadline:
            if self._tcp_port_open(timeout=min(0.5, deadline - time.monotonic())):
                logger.debug("TCP port %d is open on %s", self.port, self.host)
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationTimeoutError(
                    f"wait_for_ssh({self.host}:{self.port}): port never opened",
                    timeout,
                )
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * _WAIT_BACKOFF_FACTOR, interval)

        # Phase 2: Establish persistent paramiko connection. The TCP port is
        # already open, but sshd may still be mid-startup (and on QEMU user-mode
        # NAT the forwarder accepts the connection before sshd is up), so retry
        # the full handshake with the same tight-then-relaxing backoff.
        last_error: str = ""
        backoff = _WAIT_BACKOFF_START
        while time.monotonic() < deadline:
            try:
                self._client = self._connect()
                logger.info("SSH is ready on %s:%d", self.host, self.port)
                return
            except SmolVMError as e:
                last_error = str(e)
                logger.debug(
                    "SSH connect failed on %s:%d: %s",
                    self.host,
                    self.port,
                    last_error,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * _WAIT_BACKOFF_FACTOR, interval)

        raise OperationTimeoutError(
            f"wait_for_ssh({self.host}:{self.port}): last error: {last_error}",
            timeout,
        )

    def wait_ready(self, timeout: float = 60.0, interval: float = 0.1) -> None:
        """Block until the guest is reachable (:class:`CommChannel` alias).

        Delegates to :meth:`wait_for_ssh`; for the SSH transport "ready"
        means sshd is answering. See :class:`smolvm.comm.base.CommChannel`.
        """
        self.wait_for_ssh(timeout=timeout, interval=interval)
