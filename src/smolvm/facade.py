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
import platform
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from smolvm.backends import BACKEND_QEMU, resolve_backend
from smolvm.env import inject_env_vars, read_env_vars, remove_env_vars
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
_LOCAL_FORWARD_PROBE_TIMEOUT = 2.0
_LOCAL_FORWARD_PROBE_INTERVAL = 0.2
_LOCAL_TUNNEL_START_TIMEOUT = 10.0
_LOCAL_FORWARD_MAX_PORT_ATTEMPTS = 10


@dataclass(slots=True)
class _LocalForward:
    """Internal tracking for localhost exposure transport."""

    host_port: int
    guest_port: int
    transport: Literal["iptables", "ssh_tunnel"]
    tunnel_proc: subprocess.Popen[str] | None = None


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
        backend: Runtime backend override (``firecracker``, ``qemu``, or ``auto``).
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
        backend: str | None = None,
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

            resolved_backend = resolve_backend(backend)
            boot_args = SSH_BOOT_ARGS
            if resolved_backend == BACKEND_QEMU:
                arch = platform.machine().lower()
                console = "ttyAMA0" if arch in {"arm64", "aarch64"} else "ttyS0"
                boot_args = f"console={console} reboot=k panic=1 init=/init"

            # 1. Ensure SSH keys
            priv_key, pub_key = ensure_ssh_key()
            if ssh_key_path is None:
                ssh_key_path = str(priv_key)

            # 2. Ensure Image
            builder = ImageBuilder()
            # Keep backend/arch specific cache names to avoid stale cross-arch reuse.
            image_name = "alpine-ssh-key"
            if resolved_backend == BACKEND_QEMU:
                arch = platform.machine().lower()
                image_arch = "aarch64" if arch in {"arm64", "aarch64"} else "x86_64"
                image_name = f"alpine-ssh-key-{image_arch}"

            # This will download/build if needed (cached otherwise)
            kernel, rootfs = builder.build_alpine_ssh_key(pub_key, name=image_name)

            # 3. Create Config
            # Use a unique ID to avoid conflicts with previous runs
            auto_id = f"vm-{uuid.uuid4().hex[:8]}"
            config = VMConfig(
                vm_id=auto_id,
                vcpu_count=1,
                mem_size_mib=512,
                kernel_path=kernel,
                rootfs_path=rootfs,
                boot_args=boot_args,
                backend=resolved_backend,
            )
            logger.info("Auto-configured VM: %s (backend=%s)", auto_id, resolved_backend)

        self._ssh_user = ssh_user
        self._ssh_key_path = ssh_key_path

        sdk_kwargs: dict[str, Any] = {}
        if data_dir is not None:
            sdk_kwargs["data_dir"] = data_dir
        if socket_dir is not None:
            sdk_kwargs["socket_dir"] = socket_dir
        if backend is not None:
            sdk_kwargs["backend"] = backend

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
        self._local_forwards: dict[tuple[int, int], _LocalForward] = {}

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
        backend: str | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
    ) -> VM:
        """Reconnect to an existing VM by ID.

        Args:
            vm_id: VM identifier.
            data_dir: Override the default data directory.
            socket_dir: Override the default socket directory.
            backend: Runtime backend override (``firecracker``, ``qemu``, or ``auto``).
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
            backend=backend,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, boot_timeout: float = 30.0) -> VM:
        """Start the VM.

        If the VM config contains ``env_vars``, they are injected into
        the guest via SSH after boot completes.

        Args:
            boot_timeout: Maximum seconds to wait for boot.

        Returns:
            ``self`` for method chaining.

        Raises:
            SmolVMError: If ``env_vars`` is set but the image does not
                support SSH (missing ``init=/init`` in boot args).
        """
        if self._info.status == VMState.RUNNING:
            logger.info("VM %s already running; start() is a no-op", self._vm_id)
            return self

        self._info = self._sdk.start(self._vm_id, boot_timeout=boot_timeout)
        self._ssh_ready = False
        logger.info("VM %s started", self._vm_id)

        # Inject environment variables after boot if configured.
        env_vars = self._info.config.env_vars
        if env_vars:
            if not self.can_run_commands():
                raise SmolVMError(
                    "Cannot inject environment variables: VM image does not "
                    "support SSH (boot args missing 'init=/init'). Use an "
                    "SSH-capable image built with ImageBuilder, or bake env "
                    "vars into the rootfs at build time.",
                    {"vm_id": self._vm_id},
                )
            self.wait_for_ssh(timeout=boot_timeout)
            if self._ssh is None:
                self._ssh = SSHClient(
                    host=self._info.network.guest_ip,
                    user=self._ssh_user,
                    key_path=self._ssh_key_path,
                )
                self._ssh_ready = True
            injected = inject_env_vars(self._ssh, env_vars)
            logger.info(
                "VM %s: injected %d env var(s): %s",
                self._vm_id,
                len(injected),
                ", ".join(injected),
            )

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

    def run(
        self,
        command: str,
        timeout: int = 30,
        shell: Literal["login", "raw"] = "login",
    ) -> CommandResult:
        """Execute a command on the guest via SSH.

        Lazily creates an :class:`~smolvm.ssh.SSHClient` on first call
        and reuses it for subsequent invocations.

        Args:
            command: Shell command to execute.
            timeout: Maximum seconds to wait for the command.
            shell: Command execution mode:
                - ``"login"`` (default): run via guest login shell.
                - ``"raw"``: execute command directly with no shell wrapping.

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

        if not self._ssh_ready:
            try:
                self._wait_for_ssh_with_fallback(timeout=_DEFAULT_RUN_READY_TIMEOUT)
            except OperationTimeoutError as e:
                raise CommandExecutionUnavailableError(
                    vm_id=self._vm_id,
                    reason="SSH did not become ready on the guest.",
                    remediation=self._command_exec_remediation(),
                ) from e

        if self._ssh is None:
            raise SmolVMError(
                "Cannot run command: SSH client is not initialized",
                {"vm_id": self._vm_id},
            )

        return self._ssh.run(command, timeout=timeout, shell=shell)

    def set_env_vars(self, env_vars: dict[str, str], *, merge: bool = True) -> list[str]:
        """Set environment variables on a running VM.

        Variables are persisted in ``/etc/profile.d/smolvm_env.sh`` and
        affect new SSH sessions/login shells.

        Args:
            env_vars: Key/value pairs to set.
            merge: If True (default), merge with existing variables.

        Returns:
            Sorted variable names present after update.
        """
        if not env_vars:
            return []

        ssh = self._ensure_ssh_for_env()
        return inject_env_vars(ssh, env_vars, merge=merge)

    def unset_env_vars(self, keys: list[str]) -> dict[str, str]:
        """Remove environment variables from a running VM.

        Args:
            keys: Variable names to remove.

        Returns:
            Mapping of removed keys to their previous values.
        """
        if not keys:
            return {}

        ssh = self._ensure_ssh_for_env()
        return remove_env_vars(ssh, keys)

    def list_env_vars(self) -> dict[str, str]:
        """Return SmolVM-managed environment variables for a running VM."""
        ssh = self._ensure_ssh_for_env()
        return read_env_vars(ssh)

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

        self._wait_for_ssh_with_fallback(timeout=timeout)
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

        requested_port = host_port
        if host_port is None:
            host_port = self._allocate_local_port()
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")

        initial_key = (host_port, guest_port)
        existing = self._local_forwards.get(initial_key)
        if existing is not None:
            return existing.host_port

        candidate_ports = [host_port]
        fallback_port = self._allocate_local_port({host_port})
        if fallback_port != host_port:
            candidate_ports.append(fallback_port)

        guest_ip = self._info.network.guest_ip
        attempts: list[str] = []

        for candidate in candidate_ports:
            key = (candidate, guest_port)
            existing = self._local_forwards.get(key)
            if existing is not None:
                return existing.host_port

            if any(forward.host_port == candidate for forward in self._local_forwards.values()):
                attempts.append(f"localhost:{candidate} already exposed by this VM instance")
                continue

            iptables_configured = False
            keep_iptables = False
            try:
                self._sdk.network.setup_local_port_forward(
                    vm_id=self._vm_id,
                    guest_ip=guest_ip,
                    host_port=candidate,
                    guest_port=guest_port,
                )
                iptables_configured = True
                if self._probe_local_forward(candidate):
                    self._local_forwards[key] = _LocalForward(
                        host_port=candidate,
                        guest_port=guest_port,
                        transport="iptables",
                    )
                    keep_iptables = True
                    logger.info(
                        "VM %s exposed localhost:%d -> guest:%d (transport=iptables)",
                        self._vm_id,
                        candidate,
                        guest_port,
                    )
                    return candidate
                attempts.append(
                    f"iptables forward localhost:{candidate} -> guest:{guest_port} "
                    "was configured but not reachable"
                )
            except Exception as e:
                attempts.append(
                    f"iptables forward localhost:{candidate} -> guest:{guest_port} failed: {e}"
                )
            finally:
                if iptables_configured and not keep_iptables:
                    with suppress(Exception):
                        self._sdk.network.cleanup_local_port_forward(
                            vm_id=self._vm_id,
                            guest_ip=guest_ip,
                            host_port=candidate,
                            guest_port=guest_port,
                        )

            try:
                tunnel_proc = self._start_local_tunnel(
                    host_port=candidate,
                    guest_port=guest_port,
                )
                self._local_forwards[key] = _LocalForward(
                    host_port=candidate,
                    guest_port=guest_port,
                    transport="ssh_tunnel",
                    tunnel_proc=tunnel_proc,
                )
                logger.info(
                    "VM %s exposed localhost:%d -> guest:%d (transport=ssh_tunnel)",
                    self._vm_id,
                    candidate,
                    guest_port,
                )
                return candidate
            except Exception as e:
                attempts.append(
                    f"ssh tunnel localhost:{candidate} -> guest:{guest_port} failed: {e}"
                )
                continue

        context = {
            "vm_id": self._vm_id,
            "guest_port": guest_port,
            "requested_host_port": requested_port,
            "candidate_ports": candidate_ports,
            "attempts": attempts,
        }
        details = "; ".join(attempts) if attempts else "no attempts executed"
        raise SmolVMError(
            f"Failed to expose guest port {guest_port} on localhost. {details}",
            context,
        )

    def unexpose_local(self, host_port: int, guest_port: int) -> VM:
        """Remove a previously configured localhost-only port forward."""
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        key = (host_port, guest_port)
        tracked = self._local_forwards.pop(key, None)
        if tracked is not None:
            self._cleanup_local_forward(tracked)
            return self

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

    def close(self) -> None:
        """Release underlying SDK resources for this facade instance."""
        self._sdk.close()

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
            self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_ssh_for_env(self) -> SSHClient:
        """Return a ready SSH client for env operations on a running VM."""
        self._refresh_info()

        if self._info.status != VMState.RUNNING:
            raise SmolVMError(
                f"Cannot manage environment variables: VM is {self._info.status.value}",
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
                "Cannot manage environment variables: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        if not self._ssh_ready:
            self._wait_for_ssh_with_fallback(timeout=_DEFAULT_RUN_READY_TIMEOUT)

        if self._ssh is None:
            raise SmolVMError(
                "Cannot manage environment variables: SSH client is not initialized",
                {"vm_id": self._vm_id},
            )

        return self._ssh

    def _is_port_reachable(self, host: str, port: int, timeout: float = 0.5) -> bool:
        """Check if a TCP port is open."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (OSError, TimeoutError):
            return False

    def _refresh_info(self) -> None:
        """Refresh the cached VMInfo from the state store."""
        self._info = self._sdk.get(self._vm_id)

    def _ssh_endpoint(self) -> tuple[str, int]:
        """Return the preferred host/port endpoint for guest SSH.

        If localhost port forwarding is configured but unreachable
        (e.g. due to Linux networking issues), falls back to direct
        guest IP. The result is cached for the VM session lifetime.
        """
        # Return cached endpoint if available and valid
        if hasattr(self, "_probed_endpoint") and self._probed_endpoint:
            return self._probed_endpoint

        candidates = self._ssh_endpoints()
        if not candidates:
            raise SmolVMError(
                "No SSH endpoints available",
                {"vm_id": self._vm_id},
            )

        # Probe candidates in order
        for host, port in candidates:
            # If 127.0.0.1, we probe to ensure forwarding works.
            # If it's a direct IP, we assume it's the fallback.
            # (We probe all to be safe, with short timeout)
            if self._is_port_reachable(host, port, timeout=0.2):
                self._probed_endpoint = (host, port)
                return (host, port)

        # If none reachable (e.g. boot not finished), return the first one
        # to let standard SSH retries handle it.
        return candidates[0]

    def _ssh_endpoints(self) -> list[tuple[str, int]]:
        """Return SSH endpoint candidates in preferred order."""
        if self._info.network is None:
            raise SmolVMError(
                "VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        endpoints: list[tuple[str, int]] = []
        ssh_host_port = self._info.network.ssh_host_port
        if isinstance(ssh_host_port, int):
            endpoints.append(("127.0.0.1", ssh_host_port))
        endpoints.append((self._info.network.guest_ip, 22))

        unique: list[tuple[str, int]] = []
        for endpoint in endpoints:
            if endpoint in unique:
                continue
            unique.append(endpoint)
        return unique

    def _wait_for_ssh_with_fallback(self, timeout: float) -> None:
        """Wait for SSH, falling back from localhost-forwarded port to guest IP."""
        endpoints = self._ssh_endpoints()

        # Prefer the already selected client first, then try remaining candidates.
        if self._ssh is not None:
            current = (self._ssh.host, self._ssh.port)
            ordered = [current]
            ordered.extend(endpoint for endpoint in endpoints if endpoint != current)
            endpoints = ordered

        if len(endpoints) == 1:
            host, port = endpoints[0]
            client = self._ssh
            if client is None or client.host != host or client.port != port:
                client = SSHClient(
                    host=host,
                    user=self._ssh_user,
                    port=port,
                    key_path=self._ssh_key_path,
                )
            client.wait_for_ssh(timeout=timeout)
            self._ssh = client
            self._ssh_ready = True
            return

        errors: list[str] = []
        deadline = time.monotonic() + timeout

        for index, (host, port) in enumerate(endpoints):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            attempts_left = len(endpoints) - index
            endpoint_timeout = max(0.5, remaining / attempts_left)
            client = self._ssh
            if client is None or client.host != host or client.port != port:
                client = SSHClient(
                    host=host,
                    user=self._ssh_user,
                    port=port,
                    key_path=self._ssh_key_path,
                )

            try:
                client.wait_for_ssh(timeout=endpoint_timeout)
                self._ssh = client
                self._ssh_ready = True
                return
            except OperationTimeoutError as e:
                errors.append(f"{host}:{port} ({e.message})")

        self._ssh_ready = False
        detail = "; ".join(errors) if errors else "no endpoint attempts completed"
        raise OperationTimeoutError(f"wait_for_ssh fallback: {detail}", timeout)

    def _cleanup_local_forwards(self) -> None:
        """Best-effort cleanup for localhost-only guest port forwards."""
        if not self._local_forwards:
            return

        guest_ip: str | None = None
        with suppress(Exception):
            self._refresh_info()
            if self._info.network is not None:
                guest_ip = self._info.network.guest_ip

        for key, tracked in list(self._local_forwards.items()):
            try:
                self._cleanup_local_forward(tracked, guest_ip=guest_ip)
            except Exception:
                logger.warning(
                    "Failed to cleanup local forward localhost:%d -> guest:%d for VM %s",
                    tracked.host_port,
                    tracked.guest_port,
                    self._vm_id,
                )
            finally:
                self._local_forwards.pop(key, None)

    @staticmethod
    def _find_available_local_port() -> int:
        """Return an available TCP port bound on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _allocate_local_port(self, exclude: set[int] | None = None) -> int:
        """Allocate an unused localhost port not already tracked."""
        excluded = set(exclude or ())
        used = {forward.host_port for forward in self._local_forwards.values()}

        for _ in range(_LOCAL_FORWARD_MAX_PORT_ATTEMPTS):
            candidate = self._find_available_local_port()
            if candidate in excluded or candidate in used:
                continue
            return candidate

        raise SmolVMError(
            "Failed to allocate an available localhost port",
            {"vm_id": self._vm_id, "excluded_ports": sorted(excluded | used)},
        )

    @staticmethod
    def _probe_local_forward(host_port: int, timeout: float = _LOCAL_FORWARD_PROBE_TIMEOUT) -> bool:
        """Check whether localhost:host_port is currently accepting TCP connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=0.5):
                    return True
            except OSError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_LOCAL_FORWARD_PROBE_INTERVAL, remaining))
        return False

    def _start_local_tunnel(self, host_port: int, guest_port: int) -> subprocess.Popen[str]:
        """Start an SSH localhost tunnel from host_port to guest_port."""
        if self._info.network is None:
            raise SmolVMError(
                "Cannot create SSH tunnel: VM has no network configuration",
                {"vm_id": self._vm_id},
            )

        ssh_host, ssh_port = self._ssh_endpoint()
        cmd = [
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            "-p",
            str(ssh_port),
            "-L",
            f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
        ]
        if self._ssh_key_path:
            cmd.extend(["-i", self._ssh_key_path])
        cmd.append(f"{self._ssh_user}@{ssh_host}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            raise SmolVMError(
                "ssh binary not found. Install openssh-client.",
                {"vm_id": self._vm_id},
            ) from None
        except OSError as e:
            raise SmolVMError(
                f"Failed to start SSH tunnel: {e}",
                {"vm_id": self._vm_id},
            ) from e

        deadline = time.monotonic() + _LOCAL_TUNNEL_START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = ""
                if proc.stderr is not None:
                    with suppress(Exception):
                        stderr = proc.stderr.read().strip()
                raise SmolVMError(
                    "SSH tunnel exited before becoming ready."
                    + (f" stderr: {stderr}" if stderr else ""),
                    {"vm_id": self._vm_id, "host_port": host_port, "guest_port": guest_port},
                )
            if self._probe_local_forward(host_port, timeout=0.3):
                return proc
            time.sleep(_LOCAL_FORWARD_PROBE_INTERVAL)

        self._stop_local_tunnel(proc)
        raise SmolVMError(
            f"SSH tunnel did not become ready on localhost:{host_port} within "
            f"{_LOCAL_TUNNEL_START_TIMEOUT:.1f}s",
            {"vm_id": self._vm_id, "host_port": host_port, "guest_port": guest_port},
        )

    @staticmethod
    def _stop_local_tunnel(proc: subprocess.Popen[str] | None) -> None:
        """Best-effort shutdown for an SSH tunnel process."""
        if proc is None:
            return

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(Exception):
                    proc.wait(timeout=2.0)

        if proc.stderr is not None:
            with suppress(Exception):
                proc.stderr.close()

    def _cleanup_local_forward(
        self,
        forward: _LocalForward,
        *,
        guest_ip: str | None = None,
    ) -> None:
        """Remove one tracked localhost exposure."""
        if forward.transport == "ssh_tunnel":
            self._stop_local_tunnel(forward.tunnel_proc)
            return

        if guest_ip is None:
            with suppress(Exception):
                self._refresh_info()
                if self._info.network is not None:
                    guest_ip = self._info.network.guest_ip

        if guest_ip is None:
            logger.warning(
                "Skipping iptables cleanup for localhost:%d -> guest:%d on VM %s "
                "because guest network info is unavailable",
                forward.host_port,
                forward.guest_port,
                self._vm_id,
            )
            return

        self._sdk.network.cleanup_local_port_forward(
            vm_id=self._vm_id,
            guest_ip=guest_ip,
            host_port=forward.host_port,
            guest_port=forward.guest_port,
        )

    def _command_exec_remediation(self) -> str:
        """Return actionable guidance when command execution is unavailable."""
        return (
            "Use `VM()` auto-config mode for an SSH-ready image, or build one with "
            "`ImageBuilder.build_alpine_ssh_key(...)` and set `boot_args=SSH_BOOT_ARGS`."
        )

    def __repr__(self) -> str:
        return f"VM(vm_id={self._vm_id!r}, status={self._info.status.value!r})"
