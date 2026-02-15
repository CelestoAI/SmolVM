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

"""Low-level SmolVM manager.

Orchestrates VM lifecycle, networking, and state management across runtimes.
"""

import logging
import os
import platform
import pwd
import shlex
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from smolvm.api import FirecrackerClient
from smolvm.backends import BACKEND_FIRECRACKER, BACKEND_QEMU, resolve_backend
from smolvm.exceptions import (
    SmolVMError,
    VMNotFoundError,
)
from smolvm.host import HostCapability, HostManager
from smolvm.network import NetworkManager, check_network_prerequisites
from smolvm.storage import StateManager
from smolvm.types import NetworkConfig, VMConfig, VMInfo, VMState
from smolvm.utils import RUNTIME_PRIVILEGE_SETUP_HINT, which

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DATA_DIR_ENV = "SMOLVM_DATA_DIR"
DEFAULT_SYSTEM_DATA_DIR = Path("/var/lib/smolvm")
DEFAULT_DATA_DIR = DEFAULT_SYSTEM_DATA_DIR  # Backward-compatible constant.
DEFAULT_SOCKET_DIR = Path("/tmp")

# Backend-specific defaults
QEMU_GUEST_IP = "10.0.2.15"
QEMU_GATEWAY_IP = "10.0.2.2"
QEMU_NETMASK = "255.255.255.0"


def _get_sudo_user_info() -> pwd.struct_passwd | None:
    """Return sudo user's passwd entry when running under sudo."""
    if os.geteuid() != 0:
        return None

    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or sudo_user == "root":
        return None

    try:
        return pwd.getpwnam(sudo_user)
    except KeyError:
        return None


def _is_usable_dir(path: Path) -> bool:
    """Return True if *path* exists/is creatable and writable."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    return os.access(path, os.W_OK | os.X_OK)


def _candidate_data_dirs() -> list[Path]:
    """Build candidate data directories from most to least preferred."""
    sudo_user = _get_sudo_user_info()
    if sudo_user is not None:
        user_home = Path(sudo_user.pw_dir)
        xdg_state_home = user_home / ".local" / "state"
    else:
        user_home = Path.home()
        xdg_state_home_env = os.environ.get("XDG_STATE_HOME")
        if xdg_state_home_env:
            xdg_state_home = Path(xdg_state_home_env).expanduser()
        else:
            xdg_state_home = user_home / ".local" / "state"

    user_state_dir = xdg_state_home / "smolvm"
    legacy_user_state_dir = user_home / ".smolvm" / "state"

    if os.geteuid() == 0 and sudo_user is None:
        # Direct root session: keep system path first.
        return [DEFAULT_SYSTEM_DATA_DIR, user_state_dir, legacy_user_state_dir]

    return [user_state_dir, legacy_user_state_dir, DEFAULT_SYSTEM_DATA_DIR]


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    """Resolve and ensure the data directory path.

    Priority:
    1) Explicit ``data_dir`` argument.
    2) ``SMOLVM_DATA_DIR`` environment override.
    3) Auto-detected writable defaults (user state dir first for dev UX).
    """
    if data_dir is not None:
        resolved = data_dir.expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    env_override = os.environ.get(DEFAULT_DATA_DIR_ENV)
    if env_override:
        resolved = Path(env_override).expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    for candidate in _candidate_data_dirs():
        candidate = candidate.expanduser()
        if _is_usable_dir(candidate):
            return candidate

    attempted = ", ".join(str(p) for p in _candidate_data_dirs())
    raise PermissionError(
        f"Unable to find a writable SmolVM data directory. Tried: {attempted}. "
        f"Set {DEFAULT_DATA_DIR_ENV} to an explicit writable path."
    )


class SmolVMManager:
    """Low-level manager class for orchestrating sandbox VMs.

    Provides high-level operations for creating, starting, stopping,
    and managing VMs with proper state persistence and cleanup.
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
    ) -> None:
        """Initialize the SmolVM manager.

        Args:
            data_dir: Directory for state database. If omitted, SmolVM resolves
                a writable default (``$SMOLVM_DATA_DIR`` -> user state dir ->
                ``/var/lib/smolvm`` as fallback).
            socket_dir: Directory for VM sockets (default: /tmp).
            backend: Runtime backend (``firecracker``, ``qemu``, or ``auto``).
                Defaults to ``auto`` via :func:`smolvm.backends.resolve_backend`.
        """
        self.data_dir = resolve_data_dir(data_dir)
        self.socket_dir = socket_dir or DEFAULT_SOCKET_DIR
        self.backend = resolve_backend(backend)
        self.disk_dir = self.data_dir / "disks"
        self.disk_dir.mkdir(parents=True, exist_ok=True)

        # Under sudo, keep the chosen user-state path owned by the real user.
        owner = _get_sudo_user_info()
        if owner is not None:
            self._ensure_path_owner(self.data_dir, owner.pw_uid, owner.pw_gid)
            self._ensure_path_owner(self.disk_dir, owner.pw_uid, owner.pw_gid)

        # Initialize managers
        db_path = self.data_dir / "smolvm.db"
        self.state = StateManager(db_path)
        if owner is not None:
            self._ensure_path_owner(db_path, owner.pw_uid, owner.pw_gid)
        self.network = NetworkManager()
        self.host = HostManager()

        # Track open log file handles per VM for proper cleanup
        self._log_files: dict[str, TextIO] = {}
        self._closed = False

        logger.info(
            "SmolVM initialized: data_dir=%s, socket_dir=%s, backend=%s",
            self.data_dir,
            self.socket_dir,
            self.backend,
        )

    @staticmethod
    def _ensure_path_owner(path: Path, uid: int, gid: int) -> None:
        """Best-effort ownership alignment for sudo-created paths."""
        try:
            if not path.exists():
                return
            st = path.stat()
            if st.st_uid == uid and st.st_gid == gid:
                return
            os.chown(path, uid, gid)
        except OSError as exc:
            logger.debug("Could not update ownership for %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Lifecycle & class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_id(cls, vm_id: str, **kwargs: Any) -> "SmolVMManager":
        """Create an SDK instance and verify a VM exists.

        Useful for attaching to an existing VM from a different process.

        Args:
            vm_id: The VM identifier to look up.
            **kwargs: Forwarded to :meth:`__init__` (e.g. ``data_dir``).

        Returns:
            A :class:`SmolVMManager` instance whose state DB contains *vm_id*.

        Raises:
            VMNotFoundError: If no VM with this ID exists.
        """
        sdk = cls(**kwargs)
        sdk.state.get_vm(vm_id)  # raises VMNotFoundError if absent
        return sdk

    def __enter__(self) -> "SmolVMManager":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Release all resources held by this manager.

        Closes open log-file handles.  Calling ``close()`` more than once
        is safe (idempotent).
        """
        if self._closed:
            return
        for fh in self._log_files.values():
            with suppress(Exception):
                fh.close()
        self._log_files.clear()
        self._closed = True
        logger.debug("SmolVM resources released")

    def _resolve_vm_backend(self, backend_override: str | None) -> str:
        """Resolve a backend override with helpful error wrapping."""
        try:
            return resolve_backend(backend_override)
        except ValueError as e:
            raise SmolVMError(str(e)) from e

    def _backend_for_config(self, config: VMConfig) -> str:
        """Return effective backend for the provided VM config."""
        if config.backend:
            return self._resolve_vm_backend(config.backend)
        return self.backend

    def _backend_for_vm(self, vm_info: VMInfo) -> str:
        """Return effective backend for a persisted VM."""
        if vm_info.config.backend:
            return self._resolve_vm_backend(vm_info.config.backend)
        return self.backend

    def _instance_disk_path(self, vm_id: str) -> Path:
        """Return managed isolated disk path for a VM ID."""
        return self.disk_dir / f"{vm_id}.ext4"

    def _materialize_rootfs(self, config: VMConfig) -> VMConfig:
        """Materialize the effective rootfs path for a VM create request.

        In ``isolated`` mode, clones the configured ``rootfs_path`` into a
        per-VM disk file under ``data_dir/disks`` (or reuses an existing one
        for the same VM ID). In ``shared`` mode, uses ``rootfs_path`` directly.
        """
        if config.disk_mode == "shared":
            return config

        instance_rootfs = self._instance_disk_path(config.vm_id)
        if not instance_rootfs.exists():
            logger.info(
                "Creating isolated disk for VM %s from %s -> %s",
                config.vm_id,
                config.rootfs_path,
                instance_rootfs,
            )
            shutil.copy2(config.rootfs_path, instance_rootfs)
        else:
            logger.info(
                "Reusing isolated disk for VM %s at %s",
                config.vm_id,
                instance_rootfs,
            )

        return config.model_copy(update={"rootfs_path": instance_rootfs})

    def _managed_disk_for_vm(self, vm_info: VMInfo | None) -> Path | None:
        """Return the managed isolated disk path for a VM if applicable."""
        if vm_info is None:
            return None
        if vm_info.config.disk_mode != "isolated":
            return None

        expected = self._instance_disk_path(vm_info.vm_id).resolve()
        actual = vm_info.config.rootfs_path.resolve()
        if actual != expected:
            return None
        return expected

    def _qemu_binary_candidates(self) -> list[str]:
        """Return architecture-aware qemu-system binary candidates."""
        arch = platform.machine().lower()
        if arch in {"arm64", "aarch64"}:
            return ["qemu-system-aarch64", "qemu-system-x86_64"]
        if arch in {"x86_64", "amd64"}:
            return ["qemu-system-x86_64", "qemu-system-aarch64"]
        return ["qemu-system-aarch64", "qemu-system-x86_64"]

    def _find_qemu_binary(self) -> Path | None:
        """Find an available qemu-system binary."""
        for candidate in self._qemu_binary_candidates():
            path = which(candidate)
            if path is not None:
                return path
        return None

    def _check_qemu_prerequisites(self) -> list[str]:
        """Check host prerequisites for the qemu backend."""
        errors: list[str] = []

        qemu_bin = self._find_qemu_binary()
        if qemu_bin is None:
            errors.append(
                "QEMU not found. Install one of: qemu-system-aarch64, qemu-system-x86_64 "
                "(macOS/Homebrew: brew install qemu)."
            )
        elif platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    [str(qemu_bin), "-accel", "help"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                accel_text = f"{result.stdout}\n{result.stderr}".lower()
                if "hvf" not in accel_text:
                    errors.append(
                        "QEMU binary does not report Hypervisor.framework (hvf) acceleration. "
                        "Install/upgrade Homebrew qemu."
                    )
            except Exception:
                logger.debug("Could not probe qemu accelerators", exc_info=True)

        if which("ssh") is None:
            errors.append("'ssh' command not found (install openssh-client)")

        return errors

    def check_prerequisites(self) -> list[str]:
        """Check if all prerequisites are met.

        Uses backend-specific validation:
        - firecracker: KVM + Linux networking + Firecracker binary
        - qemu: qemu-system binary + SSH client

        Returns:
            List of error messages (empty if all good).
        """
        if self.backend == BACKEND_QEMU:
            return self._check_qemu_prerequisites()

        errors = []

        host_info = self.host.validate()

        # KVM check
        if not host_info.capabilities.get(HostCapability.KVM, False):
            errors.append("/dev/kvm not available. Try: sudo usermod -aG kvm $USER")

        # Firecracker check
        if not host_info.capabilities.get(HostCapability.FIRECRACKER, False):
            errors.append("'firecracker' binary not found in PATH or ~/.smolvm/bin/")

        # Dependency checks
        errors.extend(host_info.missing_deps)

        # Network prerequisites
        errors.extend(check_network_prerequisites())

        return errors

    def create(self, config: VMConfig) -> VMInfo:
        """Create a new microVM.

        This allocates resources (IP, TAP device) and persists state,
        but does not start the VM.

        Args:
            config: VM configuration.

        Returns:
            VMInfo for the created VM.

        Raises:
            VMAlreadyExistsError: If VM ID already exists.
            NetworkError: If network setup fails.
            ValidationError: If config is invalid.
        """
        if config is None:
            raise ValueError("config cannot be None")

        backend = self._backend_for_config(config)
        effective_config = config
        if effective_config.backend != backend:
            effective_config = effective_config.model_copy(update={"backend": backend})

        effective_config = self._materialize_rootfs(effective_config)

        logger.info(
            "Creating VM: %s (backend=%s, disk_mode=%s)",
            effective_config.vm_id,
            backend,
            effective_config.disk_mode,
        )

        # Create VM record
        vm_info = self.state.create_vm(effective_config)

        try:
            ssh_host_port = self.state.reserve_ssh_port(effective_config.vm_id)

            if backend == BACKEND_QEMU:
                mac_seed = (ssh_host_port % 254) + 1
                guest_mac = self.network.generate_mac(mac_seed)
                network_config = NetworkConfig(
                    guest_ip=QEMU_GUEST_IP,
                    gateway_ip=QEMU_GATEWAY_IP,
                    netmask=QEMU_NETMASK,
                    tap_device="usernet",
                    guest_mac=guest_mac,
                    ssh_host_port=ssh_host_port,
                )
                vm_info = self.state.update_vm(effective_config.vm_id, network=network_config)
                logger.info(
                    "VM created: %s (backend=%s, ssh localhost:%d)",
                    effective_config.vm_id,
                    backend,
                    ssh_host_port,
                )
                return vm_info

            # Firecracker networking: allocate an IP first, then derive
            # the TAP name from the last octet.
            guest_ip = self.state.allocate_ip(effective_config.vm_id, "pending")
            last_octet = int(guest_ip.split(".")[-1])
            tap_name = f"tap{last_octet}"

            # Update the lease with the real TAP name
            self.state.update_ip_lease_tap(effective_config.vm_id, tap_name)

            # Create and configure TAP device
            user = os.environ.get("USER", "root")
            self.network.create_tap(tap_name, user)

            # Use /32 mask to avoid subnet conflicts between multiple TAPs
            self.network.configure_tap(tap_name, netmask="32")
            self.network.add_route(guest_ip, tap_name)

            self.network.setup_nat(tap_name)
            self.network.setup_ssh_port_forward(
                vm_id=effective_config.vm_id,
                guest_ip=guest_ip,
                host_port=ssh_host_port,
            )

            # Generate MAC address from last octet
            guest_mac = self.network.generate_mac(last_octet)

            # Create network config
            network_config = NetworkConfig(
                guest_ip=guest_ip,
                gateway_ip=self.network.host_ip,
                tap_device=tap_name,
                guest_mac=guest_mac,
                ssh_host_port=ssh_host_port,
            )

            # Update VM with network info
            vm_info = self.state.update_vm(effective_config.vm_id, network=network_config)

            logger.info(
                "VM created: %s (IP: %s, TAP: %s)",
                effective_config.vm_id,
                guest_ip,
                tap_name,
            )
            return vm_info

        except Exception as e:
            # Rollback on failure
            logger.error("Failed to create VM %s: %s", effective_config.vm_id, e)
            self._cleanup_resources(effective_config.vm_id)
            # Delete the VM record that was created
            with suppress(Exception):
                self.state.delete_vm(effective_config.vm_id)
            raise

    def start(
        self,
        vm_id: str,
        boot_timeout: float = 30.0,
    ) -> VMInfo:
        """Start a microVM.

        Args:
            vm_id: The VM identifier.
            boot_timeout: Maximum seconds to wait for boot.

        Returns:
            Updated VMInfo.

        Raises:
            VMNotFoundError: If VM doesn't exist.
            SmolVMError: If VM is not in CREATED or STOPPED state.
            TimeoutError: If boot times out.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Starting VM: %s", vm_id)

        # Get current state
        vm_info = self.state.get_vm(vm_id)

        if vm_info.status not in (VMState.CREATED, VMState.STOPPED):
            raise SmolVMError(
                f"Cannot start VM in state '{vm_info.status.value}'",
                {"vm_id": vm_id, "current_status": vm_info.status.value},
            )

        if vm_info.network is None:
            raise SmolVMError(
                "VM has no network configuration",
                {"vm_id": vm_id},
            )

        backend = self._backend_for_vm(vm_info)

        if backend == BACKEND_QEMU:
            log_path = self.data_dir / f"{vm_id}.log"
            process = self._start_qemu(vm_info, log_path)

            try:
                # Ensure qemu did not crash immediately due invalid args/kernel.
                warmup_deadline = time.time() + min(boot_timeout, 2.0)
                while time.time() < warmup_deadline:
                    exit_code = process.poll()
                    if exit_code is not None:
                        raise SmolVMError(
                            f"QEMU exited early while booting VM '{vm_id}' (exit={exit_code})."
                        )
                    time.sleep(0.05)

                vm_info = self.state.update_vm(
                    vm_id,
                    status=VMState.RUNNING,
                    pid=process.pid,
                    socket_path=None,
                )
                logger.info(
                    "VM started: %s (backend=%s, PID: %d, ssh localhost:%s)",
                    vm_id,
                    backend,
                    process.pid,
                    vm_info.network.ssh_host_port if vm_info.network else "unknown",
                )
                return vm_info
            except Exception as e:
                logger.error("Failed to start VM %s: %s", vm_id, e)
                self._kill_process(process.pid)
                self.state.update_vm(vm_id, status=VMState.ERROR, clear_pid=True)
                raise

        # Firecracker path
        socket_path = self.socket_dir / f"fc-{vm_id}.sock"
        if socket_path.exists():
            self._unlink_socket(socket_path)

        # Start Firecracker process
        log_path = self.data_dir / f"{vm_id}.log"
        process = self._start_firecracker(socket_path, log_path)

        try:
            # Wait for socket and configure
            client = FirecrackerClient(socket_path)
            client.wait_for_socket(timeout=boot_timeout)

            # Configure the VM
            boot_args = self._resolve_boot_args(vm_info)
            client.set_boot_source(
                vm_info.config.kernel_path,
                boot_args,
            )
            client.set_machine_config(
                vm_info.config.vcpu_count,
                vm_info.config.mem_size_mib,
            )
            client.add_drive(
                "rootfs",
                vm_info.config.rootfs_path,
                is_root_device=True,
                is_read_only=False,
            )
            assert vm_info.network is not None
            client.add_network_interface(
                "eth0",
                vm_info.network.tap_device,
                vm_info.network.guest_mac,
            )

            # Start the instance
            client.start_instance()
            client.close()

            # Update state
            vm_info = self.state.update_vm(
                vm_id,
                status=VMState.RUNNING,
                pid=process.pid,
                socket_path=socket_path,
            )

            guest_ip = vm_info.network.guest_ip if vm_info.network else "unknown"
            logger.info(
                "VM started: %s (PID: %d, IP: %s)",
                vm_id,
                process.pid,
                guest_ip,
            )
            return vm_info

        except Exception as e:
            # Kill process on failure
            logger.error("Failed to start VM %s: %s", vm_id, e)
            self._kill_process(process.pid)
            self.state.update_vm(vm_id, status=VMState.ERROR, clear_pid=True)
            raise

    def stop(self, vm_id: str, timeout: float = 10.0) -> VMInfo:
        """Stop a running microVM.

        Args:
            vm_id: The VM identifier.
            timeout: Seconds to wait for graceful shutdown before killing.

        Returns:
            Updated VMInfo.

        Raises:
            VMNotFoundError: If VM doesn't exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Stopping VM: %s", vm_id)

        vm_info = self.state.get_vm(vm_id)

        if vm_info.status != VMState.RUNNING:
            logger.warning("VM %s is not running (status: %s)", vm_id, vm_info.status)
            return vm_info

        backend = self._backend_for_vm(vm_info)

        if backend == BACKEND_QEMU:
            if vm_info.pid and self._is_process_running(vm_info.pid):
                try:
                    os.kill(vm_info.pid, signal.SIGTERM)
                    self._wait_for_process(vm_info.pid, timeout)
                except Exception as e:
                    logger.warning("Graceful QEMU shutdown failed for %s: %s", vm_id, e)

            if vm_info.pid and self._is_process_running(vm_info.pid):
                self._kill_process(vm_info.pid)

            qemu_key = f"qemu:{vm_id}"
            fh = self._log_files.pop(qemu_key, None)
            if fh is not None:
                with suppress(Exception):
                    fh.close()

            vm_info = self.state.update_vm(
                vm_id,
                status=VMState.STOPPED,
                clear_pid=True,
            )
            logger.info("VM stopped: %s (backend=%s)", vm_id, backend)
            return vm_info

        # Firecracker graceful shutdown
        if vm_info.socket_path and vm_info.socket_path.exists():
            try:
                client = FirecrackerClient(vm_info.socket_path)
                client.send_ctrl_alt_del()
                client.close()

                # Wait for process to exit
                if vm_info.pid:
                    self._wait_for_process(vm_info.pid, timeout)

            except Exception as e:
                logger.warning("Graceful shutdown failed for %s: %s", vm_id, e)

        # Force kill if still running
        if vm_info.pid and self._is_process_running(vm_info.pid):
            self._kill_process(vm_info.pid)

        # Cleanup socket
        if vm_info.socket_path and vm_info.socket_path.exists():
            vm_info.socket_path.unlink()

        # Update state
        vm_info = self.state.update_vm(
            vm_id,
            status=VMState.STOPPED,
            clear_pid=True,
        )

        logger.info("VM stopped: %s", vm_id)
        return vm_info

    def delete(self, vm_id: str) -> None:
        """Delete a VM and all its resources.

        Args:
            vm_id: The VM identifier.

        Raises:
            VMNotFoundError: If VM doesn't exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Deleting VM: %s", vm_id)

        # Stop if running
        try:
            vm_info = self.state.get_vm(vm_id)
        except VMNotFoundError:
            # Best-effort cleanup of leaked artifacts, then propagate the lookup error.
            self._cleanup_resources(vm_id)
            raise

        if vm_info.status == VMState.RUNNING:
            self.stop(vm_id)

        # Cleanup all resources
        self._cleanup_resources(vm_id)

        # Delete from database
        self.state.delete_vm(vm_id)

        logger.info("VM deleted: %s", vm_id)

    def get(self, vm_id: str) -> VMInfo:
        """Get VM information.

        Args:
            vm_id: The VM identifier.

        Returns:
            VMInfo for the VM.

        Raises:
            VMNotFoundError: If VM doesn't exist.
        """
        return self.state.get_vm(vm_id)

    def list_vms(self, status: VMState | None = None) -> list[VMInfo]:
        """List all VMs.

        Args:
            status: Filter by status (optional).

        Returns:
            List of VMInfo objects.
        """
        return self.state.list_vms(status)

    def reconcile(self) -> list[str]:
        """Reconcile state with actual system state.

        Detects and marks stale VMs (marked running but process dead).

        Returns:
            List of VM IDs that were marked as ERROR.
        """
        stale_vm_ids = self.state.reconcile()

        # If a VM was marked stale, remove any SSH forwarding rules so host
        # ports do not continue to route into dead guests.
        for vm_id in stale_vm_ids:
            with suppress(Exception):
                vm_info = self.state.get_vm(vm_id)
                if vm_info.network and vm_info.network.ssh_host_port is not None:
                    self.network.cleanup_ssh_port_forward(
                        vm_id=vm_id,
                        guest_ip=vm_info.network.guest_ip,
                        host_port=vm_info.network.ssh_host_port,
                    )

        return stale_vm_ids

    def get_ssh_commands(
        self,
        vm_id: str,
        *,
        ssh_user: str = "root",
        key_path: str | Path | None = None,
        public_host: str | None = None,
    ) -> dict[str, str]:
        """Return ready-to-run SSH commands for a VM.

        Returns a dictionary containing:
        - ``private_ip``: connect directly to guest IP from host
        - ``localhost_port``: connect via reserved forwarded host port
        - ``public``: connect from internet (only when ``public_host`` given)
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not ssh_user:
            raise ValueError("ssh_user cannot be empty")

        vm_info = self.state.get_vm(vm_id)
        if vm_info.network is None:
            raise SmolVMError("VM has no network configuration", {"vm_id": vm_id})

        key_opt = ""
        if key_path is not None:
            key_opt = f"-i {shlex.quote(str(key_path))} "

        commands = {
            "private_ip": f"ssh {key_opt}{ssh_user}@{vm_info.network.guest_ip}".strip(),
        }

        if vm_info.network.ssh_host_port is not None:
            commands["localhost_port"] = (
                f"ssh {key_opt}-p {vm_info.network.ssh_host_port} {ssh_user}@127.0.0.1"
            ).strip()
            if public_host:
                commands["public"] = (
                    f"ssh {key_opt}-p {vm_info.network.ssh_host_port} {ssh_user}@{public_host}"
                ).strip()

        return commands

    def _start_qemu(
        self,
        vm_info: VMInfo,
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start a QEMU process for the qemu backend.

        Args:
            vm_info: VM info with persisted configuration/network.
            log_path: Path for combined stdout/stderr log output.

        Returns:
            The started QEMU process.

        Raises:
            SmolVMError: If qemu binary or required config is missing.
        """
        qemu_bin = self._find_qemu_binary()
        if qemu_bin is None:
            raise SmolVMError(
                "QEMU backend selected but no qemu-system binary was found. "
                "Install with: brew install qemu"
            )

        if vm_info.network is None or vm_info.network.ssh_host_port is None:
            raise SmolVMError("QEMU backend requires a reserved ssh_host_port in VM network config")

        ssh_port = vm_info.network.ssh_host_port
        guest_mac = vm_info.network.guest_mac.lower()
        boot_args = self._resolve_boot_args(vm_info)

        qemu_name = qemu_bin.name
        system = platform.system()

        drive_arg = f"file={vm_info.config.rootfs_path},if=none,format=raw,id=hd0"
        netdev_arg = f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22"

        cmd = [
            str(qemu_bin),
            "-smp",
            str(vm_info.config.vcpu_count),
            "-m",
            str(vm_info.config.mem_size_mib),
            "-kernel",
            str(vm_info.config.kernel_path),
            "-append",
            boot_args,
            "-drive",
            drive_arg,
            "-netdev",
            netdev_arg,
            "-nographic",
            "-no-reboot",
        ]

        if "aarch64" in qemu_name:
            machine = "virt,accel=hvf" if system == "Darwin" else "virt"
            cpu = "host" if system == "Darwin" else "cortex-a72"
            cmd.extend(
                [
                    "-machine",
                    machine,
                    "-cpu",
                    cpu,
                    "-device",
                    "virtio-blk-device,drive=hd0",
                    "-device",
                    f"virtio-net-device,netdev=net0,mac={guest_mac}",
                ]
            )
        else:
            machine = "q35,accel=hvf" if system == "Darwin" else "q35"
            cpu = "host" if system == "Darwin" else "max"
            cmd.extend(
                [
                    "-machine",
                    machine,
                    "-cpu",
                    cpu,
                    "-device",
                    "virtio-blk-pci,drive=hd0",
                    "-device",
                    f"virtio-net-pci,netdev=net0,mac={guest_mac}",
                ]
            )

        logger.debug("Starting QEMU: %s", " ".join(cmd))

        log_file = open(log_path, "w")  # noqa: SIM115 - must stay open for subprocess

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        key = f"qemu:{vm_info.vm_id}"
        self._log_files[key] = log_file

        logger.debug("Started QEMU: PID=%d, vm_id=%s", process.pid, vm_info.vm_id)
        return process

    def _start_firecracker(
        self,
        socket_path: Path,
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start the Firecracker process.

        Args:
            socket_path: Path for the API socket.
            log_path: Path for log output.

        Returns:
            The Popen process object.

        Raises:
            SmolVMError: If firecracker binary is not found.
        """
        fc_path = self.host.find_firecracker()
        if fc_path is None:
            raise SmolVMError(
                "Firecracker binary not found. "
                "Install it with: smolvm.host.HostManager().install_firecracker()"
            )

        cmd = [str(fc_path), "--api-sock", str(socket_path)]

        log_file = open(log_path, "w")  # noqa: SIM115 - must stay open for subprocess

        try:
            process = subprocess.Popen(
                cmd,
                # Detach child stdin from caller TTY so Firecracker cannot
                # mutate terminal/file flags seen by interactive REPL sessions.
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        # Derive a key from the socket path so we can close the handle later
        key = str(socket_path)
        self._log_files[key] = log_file

        logger.debug("Started Firecracker: PID=%d, socket=%s", process.pid, socket_path)
        return process

    def _unlink_socket(self, socket_path: Path) -> None:
        """Best-effort socket cleanup with sudo fallback for stale root-owned sockets."""
        try:
            socket_path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            # Old versions launched firecracker via sudo, producing root-owned sockets.
            # Try non-interactive sudo cleanup so retries can proceed.
            result = subprocess.run(
                ["sudo", "-n", "rm", "-f", str(socket_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                raise SmolVMError(
                    "Failed to remove stale Firecracker socket.\n"
                    f"Path: {socket_path}\n"
                    "Run one of the following:\n"
                    f"  sudo rm -f {socket_path}\n"
                    f"  {RUNTIME_PRIVILEGE_SETUP_HINT}\n"
                    f"sudo stderr: {stderr}"
                ) from None

    def _kill_process(self, pid: int) -> None:
        """Kill a process.

        Args:
            pid: Process ID to kill.
        """
        try:
            os.kill(pid, signal.SIGKILL)
            logger.debug("Killed process: %d", pid)
        except ProcessLookupError:
            pass  # Already dead
        except PermissionError:
            # Try with sudo
            try:
                subprocess.run(["sudo", "-n", "kill", "-9", str(pid)], check=False)
            except Exception:
                logger.warning("Failed to kill process %d", pid)

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process is running.

        Args:
            pid: Process ID to check.

        Returns:
            True if running.
        """
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Can't signal but exists

    def _wait_for_process(self, pid: int, timeout: float) -> None:
        """Wait for a process to exit.

        Args:
            pid: Process ID to wait for.
            timeout: Maximum seconds to wait.
        """
        start = time.time()
        while time.time() - start < timeout:
            if not self._is_process_running(pid):
                return
            time.sleep(0.1)

    def _resolve_boot_args(self, vm_info: VMInfo) -> str:
        """Resolve final boot args, injecting static IP config when absent."""
        args = vm_info.config.boot_args.strip()
        parts = args.split()

        backend = self._backend_for_vm(vm_info)
        if backend == BACKEND_QEMU:
            # Firecracker defaults include pci=off, which breaks QEMU PCI devices.
            parts = [part for part in parts if part != "pci=off"]
            if not any(part.startswith("root=") for part in parts):
                parts.extend(["root=/dev/vda", "rw"])
            args = " ".join(parts).strip()

        if vm_info.network is None:
            return args

        if any(part.startswith("ip=") for part in parts):
            return args

        ip_arg = (
            f"ip={vm_info.network.guest_ip}::"
            f"{vm_info.network.gateway_ip}:{vm_info.network.netmask}::eth0:off"
        )
        return f"{args} {ip_arg}".strip()

    def _cleanup_resources(self, vm_id: str) -> None:
        """Clean up resources for a VM.

        Args:
            vm_id: The VM identifier.
        """
        try:
            vm_info = None
            with suppress(VMNotFoundError):
                vm_info = self.state.get_vm(vm_id)

            backend = self.backend
            if vm_info is not None:
                with suppress(SmolVMError):
                    backend = self._backend_for_vm(vm_info)

            lease = self.state.get_ip_lease(vm_id)

            ssh_host_port: int | None = None
            guest_ip: str | None = lease[0] if lease else None
            if vm_info and vm_info.network:
                ssh_host_port = vm_info.network.ssh_host_port
                guest_ip = vm_info.network.guest_ip
            else:
                ssh_host_port = self.state.get_ssh_port(vm_id)

            if backend == BACKEND_FIRECRACKER and ssh_host_port is not None and guest_ip:
                with suppress(Exception):
                    self.network.cleanup_ssh_port_forward(
                        vm_id=vm_id,
                        guest_ip=guest_ip,
                        host_port=ssh_host_port,
                    )

            # Reconnect flows may not have in-memory local-forward state.
            # Always remove any persisted localhost forwarding rules by vm_id.
            with suppress(Exception):
                self.network.cleanup_all_local_port_forwards(vm_id)

            # Get IP lease info
            if lease:
                _, tap_device = lease

                if backend == BACKEND_FIRECRACKER:
                    # Cleanup Linux TAP/NAT only for Firecracker backend.
                    self.network.cleanup_nat_rules(tap_device)
                    self.network.cleanup_tap(tap_device)

                # Release IP lease regardless of backend.
                self.state.release_ip(vm_id)

            if ssh_host_port is not None:
                self.state.release_ssh_port(vm_id)

            # Cleanup Firecracker socket artifacts.
            socket_path = self.socket_dir / f"fc-{vm_id}.sock"
            if socket_path.exists():
                self._unlink_socket(socket_path)

            # Close tracked log file handles.
            firecracker_key = str(socket_path)
            qemu_key = f"qemu:{vm_id}"
            for key in (firecracker_key, qemu_key):
                fh = self._log_files.pop(key, None)
                if fh is not None:
                    with suppress(Exception):
                        fh.close()

            managed_disk = self._managed_disk_for_vm(vm_info)
            if managed_disk and managed_disk.exists():
                if vm_info.config.retain_disk_on_delete:
                    logger.info(
                        "Retaining isolated disk for VM %s at %s",
                        vm_id,
                        managed_disk,
                    )
                else:
                    with suppress(Exception):
                        managed_disk.unlink()
                        logger.info("Removed isolated disk for VM %s: %s", vm_id, managed_disk)

        except Exception as e:
            logger.warning("Error during cleanup for %s: %s", vm_id, e)
