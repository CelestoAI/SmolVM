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

"""User-friendly VM facade matching the SmolVM Initial Design API.

Provides a simple ``VM`` class that wraps the lower-level :class:`~smolvm.vm.SmolVM`
manager, giving callers an instance-style interface::

    from smolvm import VM

    with VM(config) as vm:
        # VM auto-starts on context entry
        result = vm.run("uname -r")
        print(result.stdout)
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from smolvm.exceptions import (
    CommandExecutionUnavailableError,
    OperationTimeoutError,
    SmolVMError,
)
from smolvm.ssh import SSHClient
from smolvm.types import CommandResult, VMConfig, VMInfo, VMState
from smolvm.vm import SmolVM

logger = logging.getLogger(__name__)

_DEFAULT_RUN_READY_TIMEOUT = 30.0


class VM:
    """High-level interface for a single microVM.

    Create a VM with a config, reconnect to an existing one by ID,
    or call ``VM()`` for an auto-configured SSH-ready VM.

    Args:
        config: VM configuration. Mutually exclusive with *vm_id*.
            If omitted (and *vm_id* is omitted), SmolVM auto-creates
            a default SSH-capable VM configuration.
        vm_id: ID of an existing VM to reconnect to.
        data_dir: Override the default data directory.
        socket_dir: Override the default socket directory.
        ssh_user: SSH user for :meth:`run` (default ``root``).
        ssh_key_path: Optional SSH private key path.

    Raises:
        ValueError: If both *config* and *vm_id* are given.
    """

    def __init__(
        self,
        config: VMConfig | None = None,
        *,
        vm_id: str | None = None,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
    ) -> None:
        if config is not None and vm_id is not None:
            raise ValueError("Provide either config or vm_id, not both.")

        if config is None and vm_id is None:
            # Auto-configuration mode
            logger.info("No config provided; auto-configuring standard SSH VM...")

            # Avoid circular imports (these are heavy/optional dependencies)
            import uuid

            from smolvm.build import SSH_BOOT_ARGS, ImageBuilder
            from smolvm.utils import ensure_ssh_key

            # 1. Ensure SSH keys
            priv_key, pub_key = ensure_ssh_key()
            if ssh_key_path is None:
                ssh_key_path = str(priv_key)

            # 2. Ensure Image
            builder = ImageBuilder()
            # This will download/build if needed (cached otherwise)
            kernel, rootfs = builder.build_alpine_ssh_key(pub_key)

            # 3. Create Config
            # Use a unique ID to avoid conflicts with previous runs
            auto_id = f"vm-{uuid.uuid4().hex[:8]}"
            config = VMConfig(
                vm_id=auto_id,
                vcpu_count=1,
                mem_size_mib=512,
                kernel_path=kernel,
                rootfs_path=rootfs,
                boot_args=SSH_BOOT_ARGS,
            )
            logger.info("Auto-configured VM: %s", auto_id)

        self._ssh_user = ssh_user
        self._ssh_key_path = ssh_key_path

        sdk_kwargs: dict = {}
        if data_dir is not None:
            sdk_kwargs["data_dir"] = data_dir
        if socket_dir is not None:
            sdk_kwargs["socket_dir"] = socket_dir

        if config is not None:
            self._sdk = SmolVM(**sdk_kwargs)
            self._info = self._sdk.create(config)
            self._vm_id = config.vm_id
            self._owns_vm = True
        else:
            # Reconnect to an existing VM
            assert vm_id is not None
            self._sdk = SmolVM.from_id(vm_id, **sdk_kwargs)
            self._info = self._sdk.get(vm_id)
            self._vm_id = vm_id
            self._owns_vm = False

        self._ssh: SSHClient | None = None
        self._ssh_ready = False
        self._local_forwards: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_id(
        cls,
        vm_id: str,
        *,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
    ) -> VM:
        """Reconnect to an existing VM by ID.

        Args:
            vm_id: VM identifier.
            data_dir: Override the default data directory.
            socket_dir: Override the default socket directory.
            ssh_user: SSH user for :meth:`run`.
            ssh_key_path: Optional SSH private key path.

        Returns:
            A :class:`VM` instance bound to the existing VM.

        Raises:
            VMNotFoundError: If no VM with this ID exists.
        """
        return cls(
            vm_id=vm_id,
            data_dir=data_dir,
            socket_dir=socket_dir,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, boot_timeout: float = 30.0) -> VM:
        """Start the VM.

        Args:
            boot_timeout: Maximum seconds to wait for boot.

        Returns:
            ``self`` for method chaining.
        """
        if self._info.status == VMState.RUNNING:
            logger.info("VM %s already running; start() is a no-op", self._vm_id)
            return self

        self._info = self._sdk.start(self._vm_id, boot_timeout=boot_timeout)
        self._ssh_ready = False
        logger.info("VM %s started", self._vm_id)
        return self

    def stop(self, timeout: float = 10.0) -> VM:
        """Stop the VM.

        Args:
            timeout: Seconds to wait for graceful shutdown.

        Returns:
            ``self`` for method chaining.
        """
        self._cleanup_local_forwards()
        self._info = self._sdk.stop(self._vm_id, timeout=timeout)
        self._ssh = None
        self._ssh_ready = False
        logger.info("VM %s stopped", self._vm_id)
        return self

    def delete(self) -> None:
        """Delete the VM and release all resources."""
        self._cleanup_local_forwards()
        self._sdk.delete(self._vm_id)
        self._ssh = None
        self._ssh_ready = False
        logger.info("VM %s deleted", self._vm_id)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run(self, command: str, timeout: int = 30) -> CommandResult:
        """Execute a command on the guest via SSH.

        Lazily creates an :class:`~smolvm.ssh.SSHClient` on first call
        and reuses it for subsequent invocations.

        Args:
            command: Shell command to execute.
            timeout: Maximum seconds to wait for the command.

        Returns:
            :class:`~smolvm.types.CommandResult`.

        Raises:
            SmolVMError: If the VM is not running or has no network.
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Cannot run command: VM is {self._info.status.value}",
                {"vm_id": self._vm_id},
            )
        if not self.can_run_commands():
            raise CommandExecutionUnavailableError(
                vm_id=self._vm_id,
                reason=(
                    "VM boot args are missing 'init=/init', "
                    "so guest SSH is not guaranteed to start."
                ),
                remediation=self._command_exec_remediation(),
            )
        if self._info.network is None:
            raise SmolVMError(
                "Cannot run command: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        if self._ssh is None:
            self._ssh = SSHClient(
                host=self._info.network.guest_ip,
                user=self._ssh_user,
                key_path=self._ssh_key_path,
            )

        if not self._ssh_ready:
            try:
                self._ssh.wait_for_ssh(timeout=_DEFAULT_RUN_READY_TIMEOUT)
                self._ssh_ready = True
            except OperationTimeoutError as e:
                raise CommandExecutionUnavailableError(
                    vm_id=self._vm_id,
                    reason="SSH did not become ready on the guest.",
                    remediation=self._command_exec_remediation(),
                ) from e

        return self._ssh.run(command, timeout=timeout)

    def wait_for_ssh(self, timeout: float = 60.0) -> VM:
        """Wait for SSH to become available on the guest.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            ``self`` for method chaining.

        Raises:
            OperationTimeoutError: If SSH is not available in time.
            SmolVMError: If the VM is not running.
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Cannot wait for SSH: VM is {self._info.status.value}",
                {"vm_id": self._vm_id},
            )
        if self._info.network is None:
            raise SmolVMError(
                "Cannot wait for SSH: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        if self._ssh is None:
            self._ssh = SSHClient(
                host=self._info.network.guest_ip,
                user=self._ssh_user,
                key_path=self._ssh_key_path,
            )

        self._ssh.wait_for_ssh(timeout=timeout)
        self._ssh_ready = True
        return self

    def ssh_commands(
        self,
        *,
        ssh_user: str | None = None,
        key_path: str | Path | None = None,
        public_host: str | None = None,
    ) -> dict[str, str]:
        """Get ready-to-run SSH commands for this VM."""
        return self._sdk.get_ssh_commands(
            self._vm_id,
            ssh_user=ssh_user or self._ssh_user,
            key_path=key_path or self._ssh_key_path,
            public_host=public_host,
        )

    def expose_local(self, guest_port: int, host_port: int | None = None) -> int:
        """Expose a guest TCP port on localhost only.

        Forwards ``127.0.0.1:<host_port>`` on the host to
        ``<guest_ip>:<guest_port>`` inside the VM.

        Args:
            guest_port: Guest TCP port to expose.
            host_port: Host localhost port. If omitted, an available port is chosen.

        Returns:
            The host localhost port to connect to.
        """
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Cannot expose port: VM is {self._info.status.value}",
                {"vm_id": self._vm_id},
            )
        if self._info.network is None:
            raise SmolVMError(
                "Cannot expose port: VM has no network configuration",
                {"vm_id": self._vm_id},
            )
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        if host_port is None:
            host_port = self._find_available_local_port()
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")

        key = (host_port, guest_port)
        if key in self._local_forwards:
            return host_port

        if any(existing_host == host_port for existing_host, _ in self._local_forwards):
            raise SmolVMError(
                f"Host port {host_port} is already exposed for this VM instance",
                {"vm_id": self._vm_id, "host_port": host_port},
            )

        self._sdk.network.setup_local_port_forward(
            vm_id=self._vm_id,
            guest_ip=self._info.network.guest_ip,
            host_port=host_port,
            guest_port=guest_port,
        )
        self._local_forwards.add(key)
        logger.info(
            "VM %s exposed localhost:%d -> guest:%d",
            self._vm_id,
            host_port,
            guest_port,
        )
        return host_port

    def unexpose_local(self, host_port: int, guest_port: int) -> VM:
        """Remove a previously configured localhost-only port forward."""
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        self._refresh_info()
        if self._info.network is None:
            raise SmolVMError(
                "Cannot remove local port forward: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        self._sdk.network.cleanup_local_port_forward(
            vm_id=self._vm_id,
            guest_ip=self._info.network.guest_ip,
            host_port=host_port,
            guest_port=guest_port,
        )
        self._local_forwards.discard((host_port, guest_port))
        return self

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def vm_id(self) -> str:
        """The VM identifier."""
        return self._vm_id

    @property
    def info(self) -> VMInfo:
        """Current VM runtime information (cached; call :meth:`refresh` to update)."""
        return self._info

    @property
    def status(self) -> VMState:
        """Current VM lifecycle state (cached)."""
        return self._info.status

    @property
    def data_dir(self) -> Path:
        """Directory backing the VM state DB and logs."""
        return self._sdk.data_dir

    def get_ip(self) -> str:
        """Return the guest IP address.

        Raises:
            SmolVMError: If the VM has no network configuration.
        """
        self._refresh_info()
        if self._info.network is None:
            raise SmolVMError(
                "VM has no network configuration",
                {"vm_id": self._vm_id},
            )
        return self._info.network.guest_ip

    def refresh(self) -> VM:
        """Refresh cached VM info from the state store.

        Returns:
            ``self`` for method chaining.
        """
        self._refresh_info()
        return self

    def can_run_commands(self) -> bool:
        """Whether this VM config supports command execution via SSH.

        Command execution currently requires SmolVM's SSH init flow,
        which is enabled by booting with ``init=/init``.
        """
        return "init=/init" in self._info.config.boot_args

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> VM:
        # Auto-start VMs created by this facade instance so callers can
        # immediately interact with the guest inside a context block.
        if self._owns_vm and self._info.status != VMState.RUNNING:
            self.start()
        return self

    def __exit__(self, *args: object) -> None:
        try:
            # Best-effort stop on context exit.
            if self._info.status == VMState.RUNNING:
                try:
                    self.stop()
                except Exception:
                    logger.warning("Failed to stop VM %s on context exit", self._vm_id)

            # Auto-delete only for VMs created by this facade instance.
            if self._owns_vm:
                try:
                    self.delete()
                except Exception:
                    logger.warning("Failed to delete VM %s on context exit", self._vm_id)
        finally:
            self._sdk.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_info(self) -> None:
        """Refresh the cached VMInfo from the state store."""
        self._info = self._sdk.get(self._vm_id)

    def _cleanup_local_forwards(self) -> None:
        """Best-effort cleanup for localhost-only guest port forwards."""
        if not self._local_forwards:
            return

        self._refresh_info()
        if self._info.network is None:
            self._local_forwards.clear()
            return

        guest_ip = self._info.network.guest_ip
        for host_port, guest_port in list(self._local_forwards):
            try:
                self._sdk.network.cleanup_local_port_forward(
                    vm_id=self._vm_id,
                    guest_ip=guest_ip,
                    host_port=host_port,
                    guest_port=guest_port,
                )
            except Exception:
                logger.warning(
                    "Failed to cleanup local forward localhost:%d -> guest:%d for VM %s",
                    host_port,
                    guest_port,
                    self._vm_id,
                )
            finally:
                self._local_forwards.discard((host_port, guest_port))

    @staticmethod
    def _find_available_local_port() -> int:
        """Return an available TCP port bound on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _command_exec_remediation(self) -> str:
        """Return actionable guidance when command execution is unavailable."""
        return (
            "Use `VM()` auto-config mode for an SSH-ready image, or build one with "
            "`ImageBuilder.build_alpine_ssh_key(...)` and set `boot_args=SSH_BOOT_ARGS`."
        )

    def __repr__(self) -> str:
        return f"VM(vm_id={self._vm_id!r}, status={self._info.status.value!r})"
