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

"""SQLite-based state management for SmolVM.

Provides persistent storage for VM metadata, lifecycle states, and IP allocations.
Uses exclusive transactions to prevent race conditions in IP assignment.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from smolvm.exceptions import NetworkError, VMAlreadyExistsError, VMNotFoundError
from smolvm.types import NetworkConfig, VMConfig, VMInfo, VMState

logger = logging.getLogger(__name__)

# IP allocation pool: 172.16.0.2 - 172.16.0.254
IP_POOL_START = 2
IP_POOL_END = 254
IP_PREFIX = "172.16.0."

# SSH host-port forwarding pool: 2200 - 2999
SSH_PORT_START = 2200
SSH_PORT_END = 2999


class StateManager:
    """Manages persistent state for VMs and IP allocations.

    Uses SQLite for atomic operations and crash recovery.
    """

    def __init__(self, db_path: Path) -> None:
        """Initialize the state manager.

        Args:
            db_path: Path to the SQLite database file.
        """
        if db_path is None:
            raise ValueError("db_path cannot be None")

        self.db_path = db_path
        self._init_schema()
        logger.info("StateManager initialized with database: %s", db_path)

    @contextmanager
    def _get_connection(self, exclusive: bool = False) -> Iterator[sqlite3.Connection]:
        """Get a database connection with proper isolation.

        Args:
            exclusive: If True, use exclusive transaction for writes.

        Yields:
            SQLite connection with row factory set.
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level="EXCLUSIVE" if exclusive else "DEFERRED",
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """Create database tables if they don't exist."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS vms (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    config TEXT NOT NULL,
                    network TEXT,
                    pid INTEGER,
                    socket_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ip_leases (
                    ip TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL UNIQUE,
                    tap_device TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (vm_id) REFERENCES vms(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_vms_status ON vms(status);
                CREATE INDEX IF NOT EXISTS idx_ip_leases_vm_id ON ip_leases(vm_id);

                CREATE TABLE IF NOT EXISTS ssh_forwards (
                    vm_id TEXT PRIMARY KEY,
                    host_port INTEGER NOT NULL UNIQUE,
                    guest_port INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (vm_id) REFERENCES vms(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_ssh_forwards_vm_id ON ssh_forwards(vm_id);
                CREATE INDEX IF NOT EXISTS idx_ssh_forwards_host_port ON ssh_forwards(host_port);
            """)

    def create_vm(self, config: VMConfig) -> VMInfo:
        """Create a new VM record.

        Args:
            config: The VM configuration.

        Returns:
            VMInfo with CREATED status.

        Raises:
            VMAlreadyExistsError: If a VM with this ID already exists.
        """
        if config is None:
            raise ValueError("config cannot be None")

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection(exclusive=True) as conn:
            # Check for existing VM
            existing = conn.execute("SELECT id FROM vms WHERE id = ?", (config.vm_id,)).fetchone()
            if existing:
                raise VMAlreadyExistsError(config.vm_id)

            # Insert new VM
            conn.execute(
                """
                INSERT INTO vms (id, status, config, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    config.vm_id,
                    VMState.CREATED.value,
                    config.model_dump_json(),
                    now,
                    now,
                ),
            )

        logger.info("Created VM record: %s", config.vm_id)
        return VMInfo(vm_id=config.vm_id, status=VMState.CREATED, config=config)

    def get_vm(self, vm_id: str) -> VMInfo:
        """Get VM information by ID.

        Args:
            vm_id: The VM identifier.

        Returns:
            VMInfo for the VM.

        Raises:
            VMNotFoundError: If the VM does not exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM vms WHERE id = ?", (vm_id,)).fetchone()

        if not row:
            raise VMNotFoundError(vm_id)

        config = VMConfig.model_validate_json(row["config"])
        network = NetworkConfig.model_validate_json(row["network"]) if row["network"] else None
        socket_path = Path(row["socket_path"]) if row["socket_path"] else None

        return VMInfo(
            vm_id=row["id"],
            status=VMState(row["status"]),
            config=config,
            network=network,
            pid=row["pid"],
            socket_path=socket_path,
        )

    def update_vm(
        self,
        vm_id: str,
        *,
        status: VMState | None = None,
        network: NetworkConfig | None = None,
        pid: int | None = None,
        socket_path: Path | None = None,
        clear_pid: bool = False,
    ) -> VMInfo:
        """Update VM state.

        Args:
            vm_id: The VM identifier.
            status: New status (optional).
            network: Network configuration (optional).
            pid: Process ID (optional).
            socket_path: API socket path (optional).
            clear_pid: If True, set pid to NULL.

        Returns:
            Updated VMInfo.

        Raises:
            VMNotFoundError: If the VM does not exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection(exclusive=True) as conn:
            # Verify VM exists
            existing = conn.execute("SELECT id FROM vms WHERE id = ?", (vm_id,)).fetchone()
            if not existing:
                raise VMNotFoundError(vm_id)

            # Build update query dynamically
            updates = ["updated_at = ?"]
            params: list = [now]

            if status is not None:
                updates.append("status = ?")
                params.append(status.value)

            if network is not None:
                updates.append("network = ?")
                params.append(network.model_dump_json())

            if pid is not None:
                updates.append("pid = ?")
                params.append(pid)
            elif clear_pid:
                updates.append("pid = NULL")

            if socket_path is not None:
                updates.append("socket_path = ?")
                params.append(str(socket_path))

            params.append(vm_id)
            query = f"UPDATE vms SET {', '.join(updates)} WHERE id = ?"
            conn.execute(query, params)

        logger.info("Updated VM %s: status=%s, pid=%s", vm_id, status, pid)
        return self.get_vm(vm_id)

    def delete_vm(self, vm_id: str) -> None:
        """Delete a VM record.

        Args:
            vm_id: The VM identifier.

        Raises:
            VMNotFoundError: If the VM does not exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            # Verify VM exists
            existing = conn.execute("SELECT id FROM vms WHERE id = ?", (vm_id,)).fetchone()
            if not existing:
                raise VMNotFoundError(vm_id)

            # Delete (IP lease deleted via CASCADE)
            conn.execute("DELETE FROM vms WHERE id = ?", (vm_id,))

        logger.info("Deleted VM: %s", vm_id)

    def list_vms(self, status: VMState | None = None) -> list[VMInfo]:
        """List all VMs, optionally filtered by status.

        Args:
            status: Filter by this status (optional).

        Returns:
            List of VMInfo objects.
        """
        with self._get_connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM vms WHERE status = ? ORDER BY created_at",
                    (status.value,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM vms ORDER BY created_at").fetchall()

        result = []
        for row in rows:
            config = VMConfig.model_validate_json(row["config"])
            network = NetworkConfig.model_validate_json(row["network"]) if row["network"] else None
            socket_path = Path(row["socket_path"]) if row["socket_path"] else None
            result.append(
                VMInfo(
                    vm_id=row["id"],
                    status=VMState(row["status"]),
                    config=config,
                    network=network,
                    pid=row["pid"],
                    socket_path=socket_path,
                )
            )
        return result

    def allocate_ip(self, vm_id: str, tap_device: str) -> str:
        """Atomically allocate the next available IP address.

        Args:
            vm_id: The VM to allocate for.
            tap_device: The TAP device name.

        Returns:
            The allocated IP address (e.g., "172.16.0.2").

        Raises:
            NetworkError: If no IPs are available.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not tap_device:
            raise ValueError("tap_device cannot be empty")

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection(exclusive=True) as conn:
            # Find all allocated IPs
            allocated = conn.execute("SELECT ip FROM ip_leases").fetchall()
            allocated_set = {row["ip"] for row in allocated}

            # Find first available
            for i in range(IP_POOL_START, IP_POOL_END + 1):
                ip = f"{IP_PREFIX}{i}"
                if ip not in allocated_set:
                    conn.execute(
                        """
                        INSERT INTO ip_leases (ip, vm_id, tap_device, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (ip, vm_id, tap_device, now),
                    )
                    logger.info("Allocated IP %s to VM %s", ip, vm_id)
                    return ip

        raise NetworkError("No IP addresses available in pool")

    def release_ip(self, vm_id: str) -> None:
        """Release the IP allocated to a VM.

        Args:
            vm_id: The VM identifier.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            result = conn.execute("DELETE FROM ip_leases WHERE vm_id = ?", (vm_id,))
            if result.rowcount > 0:
                logger.info("Released IP for VM: %s", vm_id)

    def get_ip_lease(self, vm_id: str) -> tuple[str, str] | None:
        """Get the IP lease for a VM.

        Args:
            vm_id: The VM identifier.

        Returns:
            Tuple of (ip, tap_device) or None if no lease.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT ip, tap_device FROM ip_leases WHERE vm_id = ?",
                (vm_id,),
            ).fetchone()

        if row:
            return (row["ip"], row["tap_device"])
        return None

    def update_ip_lease_tap(self, vm_id: str, tap_device: str) -> None:
        """Update the TAP device name for an existing IP lease.

        Args:
            vm_id: The VM identifier.
            tap_device: New TAP device name.

        Raises:
            ValueError: If vm_id or tap_device is empty.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not tap_device:
            raise ValueError("tap_device cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            conn.execute(
                "UPDATE ip_leases SET tap_device = ? WHERE vm_id = ?",
                (tap_device, vm_id),
            )
            logger.debug("Updated TAP device for VM %s to %s", vm_id, tap_device)

    def reserve_ssh_port(self, vm_id: str, guest_port: int = 22) -> int:
        """Reserve a host port for forwarding to guest SSH.

        Args:
            vm_id: The VM identifier.
            guest_port: Guest-side TCP port (default: 22).

        Returns:
            Reserved host TCP port.

        Raises:
            ValueError: If vm_id is empty or guest_port invalid.
            NetworkError: If no host ports are available.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection(exclusive=True) as conn:
            existing = conn.execute(
                "SELECT host_port FROM ssh_forwards WHERE vm_id = ?",
                (vm_id,),
            ).fetchone()
            if existing:
                return int(existing["host_port"])

            allocated = conn.execute("SELECT host_port FROM ssh_forwards").fetchall()
            allocated_set = {int(row["host_port"]) for row in allocated}

            for host_port in range(SSH_PORT_START, SSH_PORT_END + 1):
                if host_port in allocated_set:
                    continue
                conn.execute(
                    """
                    INSERT INTO ssh_forwards (vm_id, host_port, guest_port, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (vm_id, host_port, guest_port, now),
                )
                logger.info("Reserved SSH host port %d for VM %s", host_port, vm_id)
                return host_port

        raise NetworkError("No SSH host ports available in pool")

    def get_ssh_port(self, vm_id: str) -> int | None:
        """Get the reserved SSH host port for a VM.

        Args:
            vm_id: The VM identifier.

        Returns:
            Reserved host port, or None if not reserved.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT host_port FROM ssh_forwards WHERE vm_id = ?",
                (vm_id,),
            ).fetchone()

        if row:
            return int(row["host_port"])
        return None

    def release_ssh_port(self, vm_id: str) -> None:
        """Release a VM's reserved SSH host port.

        Args:
            vm_id: The VM identifier.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            result = conn.execute("DELETE FROM ssh_forwards WHERE vm_id = ?", (vm_id,))
            if result.rowcount > 0:
                logger.info("Released SSH host port for VM: %s", vm_id)

    def reconcile(self) -> list[str]:
        """Check for stale VMs (marked RUNNING but process is dead).

        Returns:
            List of VM IDs that were marked as ERROR.
        """
        import os

        stale_vms = []

        with self._get_connection(exclusive=True) as conn:
            running = conn.execute(
                "SELECT id, pid FROM vms WHERE status = ?",
                (VMState.RUNNING.value,),
            ).fetchall()

            for row in running:
                pid = row["pid"]
                if pid is None:
                    # No PID recorded but marked running - stale
                    stale_vms.append(row["id"])
                    continue

                # Check if process exists
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    # Process doesn't exist
                    stale_vms.append(row["id"])
                except PermissionError:
                    # Process exists but we can't signal it - still alive
                    pass

            # Mark stale VMs as ERROR
            now = datetime.now(timezone.utc).isoformat()
            for vm_id in stale_vms:
                conn.execute(
                    """
                    UPDATE vms SET status = ?, pid = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (VMState.ERROR.value, now, vm_id),
                )
                logger.warning("Marked stale VM as ERROR: %s", vm_id)

        return stale_vms
