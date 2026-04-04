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

from smolvm.exceptions import (
    BrowserSessionAlreadyExistsError,
    BrowserSessionNotFoundError,
    NetworkError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionInfo,
    BrowserSessionState,
    NetworkConfig,
    SnapshotArtifacts,
    SnapshotInfo,
    VMConfig,
    VMInfo,
    VMState,
)

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
            conn.executescript(
                """
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

                CREATE TABLE IF NOT EXISTS browser_sessions (
                    session_id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL UNIQUE,
                    config TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cdp_url TEXT,
                    live_url TEXT,
                    debug_port INTEGER,
                    profile_id TEXT,
                    expires_at TEXT,
                    artifacts_dir TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_browser_sessions_status
                    ON browser_sessions(status);
                CREATE INDEX IF NOT EXISTS idx_browser_sessions_profile_id
                    ON browser_sessions(profile_id);

                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    mem_file_path TEXT NOT NULL,
                    disk_path TEXT NOT NULL,
                    backend TEXT,
                    artifacts TEXT,
                    vm_config TEXT NOT NULL,
                    network_config TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    restored INTEGER DEFAULT 0,
                    restored_vm_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_vm_id ON snapshots(vm_id);
            """
            )
            snapshot_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()
            }
            if "backend" not in snapshot_columns:
                conn.execute("ALTER TABLE snapshots ADD COLUMN backend TEXT")
            if "artifacts" not in snapshot_columns:
                conn.execute("ALTER TABLE snapshots ADD COLUMN artifacts TEXT")

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

        config = self._vm_config_from_json(row["config"])
        network = NetworkConfig.model_validate_json(row["network"]) if row["network"] else None
        control_socket_path = Path(row["socket_path"]) if row["socket_path"] else None

        return VMInfo(
            vm_id=row["id"],
            status=VMState(row["status"]),
            config=config,
            network=network,
            pid=row["pid"],
            control_socket_path=control_socket_path,
        )

    def update_vm(
        self,
        vm_id: str,
        *,
        status: VMState | None = None,
        network: NetworkConfig | None = None,
        pid: int | None = None,
        control_socket_path: Path | None = None,
        socket_path: Path | None = None,
        clear_pid: bool = False,
        clear_socket_path: bool = False,
    ) -> VMInfo:
        """Update VM state.

        Args:
            vm_id: The VM identifier.
            status: New status (optional).
            network: Network configuration (optional).
            pid: Process ID (optional).
            control_socket_path: Runtime control socket path (optional).
            socket_path: Deprecated alias for ``control_socket_path``.
            clear_pid: If True, set pid to NULL.
            clear_socket_path: If True, set socket_path to NULL.

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

            effective_control_socket_path = control_socket_path
            if effective_control_socket_path is None and socket_path is not None:
                effective_control_socket_path = socket_path

            if effective_control_socket_path is not None:
                updates.append("socket_path = ?")
                params.append(str(effective_control_socket_path))
            elif clear_socket_path:
                updates.append("socket_path = NULL")

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
            config = self._vm_config_from_json(row["config"])
            network = NetworkConfig.model_validate_json(row["network"]) if row["network"] else None
            control_socket_path = Path(row["socket_path"]) if row["socket_path"] else None
            result.append(
                VMInfo(
                    vm_id=row["id"],
                    status=VMState(row["status"]),
                    config=config,
                    network=network,
                    pid=row["pid"],
                    control_socket_path=control_socket_path,
                )
            )
        return result

    @staticmethod
    def _vm_config_from_json(raw: str) -> VMConfig:
        """Deserialize VM config while trusting persisted filesystem paths."""
        return VMConfig.model_validate_json(raw, context={"validate_paths": False})

    @staticmethod
    def _snapshot_vm_config_from_json(raw: str) -> VMConfig:
        """Deserialize snapshot VM config without filesystem validation."""
        return StateManager._vm_config_from_json(raw)

    @staticmethod
    def _snapshot_info_from_row(row: sqlite3.Row) -> SnapshotInfo:
        """Convert a snapshots row into SnapshotInfo."""
        backend = row["backend"] if row["backend"] else "firecracker"
        if row["artifacts"]:
            artifacts = SnapshotArtifacts.model_validate_json(row["artifacts"])
        else:
            artifacts = SnapshotArtifacts(
                state_path=Path(row["snapshot_path"]) if row["snapshot_path"] else None,
                memory_path=Path(row["mem_file_path"]) if row["mem_file_path"] else None,
                disk_path=Path(row["disk_path"]),
            )
        return SnapshotInfo(
            snapshot_id=row["snapshot_id"],
            vm_id=row["vm_id"],
            backend=backend,
            artifacts=artifacts,
            vm_config=StateManager._snapshot_vm_config_from_json(row["vm_config"]),
            network_config=NetworkConfig.model_validate_json(row["network_config"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            restored=bool(row["restored"]),
            restored_vm_id=row["restored_vm_id"],
        )

    def create_snapshot(self, info: SnapshotInfo) -> SnapshotInfo:
        """Persist snapshot metadata."""
        if info is None:
            raise ValueError("info cannot be None")

        with self._get_connection(exclusive=True) as conn:
            existing = conn.execute(
                "SELECT snapshot_id FROM snapshots WHERE snapshot_id = ?",
                (info.snapshot_id,),
            ).fetchone()
            if existing:
                raise SnapshotAlreadyExistsError(info.snapshot_id)

            conn.execute(
                """
                INSERT INTO snapshots (
                    snapshot_id,
                    vm_id,
                    snapshot_path,
                    mem_file_path,
                    disk_path,
                    backend,
                    artifacts,
                    vm_config,
                    network_config,
                    created_at,
                    restored,
                    restored_vm_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    info.snapshot_id,
                    info.vm_id,
                    str(info.artifacts.state_path) if info.artifacts.state_path else "",
                    str(info.artifacts.memory_path) if info.artifacts.memory_path else "",
                    str(info.artifacts.disk_path),
                    info.backend,
                    info.artifacts.model_dump_json(),
                    info.vm_config.model_dump_json(),
                    info.network_config.model_dump_json(),
                    info.created_at.isoformat(),
                    int(info.restored),
                    info.restored_vm_id,
                ),
            )

        logger.info("Created snapshot record: %s", info.snapshot_id)
        return info

    def get_snapshot(self, snapshot_id: str) -> SnapshotInfo:
        """Get snapshot metadata by ID."""
        if not snapshot_id:
            raise ValueError("snapshot_id cannot be empty")

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()

        if not row:
            raise SnapshotNotFoundError(snapshot_id)

        return self._snapshot_info_from_row(row)

    def list_snapshots(self, vm_id: str | None = None) -> list[SnapshotInfo]:
        """List snapshots, optionally filtered by source VM ID."""
        with self._get_connection() as conn:
            if vm_id:
                rows = conn.execute(
                    "SELECT * FROM snapshots WHERE vm_id = ? ORDER BY created_at",
                    (vm_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM snapshots ORDER BY created_at").fetchall()

        return [self._snapshot_info_from_row(row) for row in rows]

    def mark_snapshot_restored(self, snapshot_id: str, restored_vm_id: str) -> SnapshotInfo:
        """Mark a snapshot as restored."""
        if not snapshot_id:
            raise ValueError("snapshot_id cannot be empty")
        if not restored_vm_id:
            raise ValueError("restored_vm_id cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            result = conn.execute(
                """
                UPDATE snapshots
                SET restored = 1, restored_vm_id = ?
                WHERE snapshot_id = ?
                """,
                (restored_vm_id, snapshot_id),
            )
            if result.rowcount == 0:
                raise SnapshotNotFoundError(snapshot_id)

        logger.info("Marked snapshot %s as restored to VM %s", snapshot_id, restored_vm_id)
        return self.get_snapshot(snapshot_id)

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete a snapshot metadata record."""
        if not snapshot_id:
            raise ValueError("snapshot_id cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            result = conn.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            if result.rowcount == 0:
                raise SnapshotNotFoundError(snapshot_id)

        logger.info("Deleted snapshot record: %s", snapshot_id)

    @staticmethod
    def _browser_session_info_from_row(row: sqlite3.Row) -> BrowserSessionInfo:
        """Convert a browser_sessions row into BrowserSessionInfo."""
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        artifacts_dir = Path(row["artifacts_dir"]) if row["artifacts_dir"] else None
        return BrowserSessionInfo(
            session_id=row["session_id"],
            vm_id=row["vm_id"],
            status=BrowserSessionState(row["status"]),
            cdp_url=row["cdp_url"],
            live_url=row["live_url"],
            debug_port=row["debug_port"],
            profile_id=row["profile_id"],
            expires_at=expires_at,
            artifacts_dir=artifacts_dir,
        )

    def create_browser_session(
        self,
        info: BrowserSessionInfo,
        config: BrowserSessionConfig,
    ) -> BrowserSessionInfo:
        """Create a new browser session record."""
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection(exclusive=True) as conn:
            existing = conn.execute(
                "SELECT session_id FROM browser_sessions WHERE session_id = ?",
                (info.session_id,),
            ).fetchone()
            if existing:
                raise BrowserSessionAlreadyExistsError(info.session_id)

            conn.execute(
                """
                INSERT INTO browser_sessions (
                    session_id,
                    vm_id,
                    config,
                    status,
                    cdp_url,
                    live_url,
                    debug_port,
                    profile_id,
                    expires_at,
                    artifacts_dir,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    info.session_id,
                    info.vm_id,
                    config.model_dump_json(),
                    info.status.value,
                    info.cdp_url,
                    info.live_url,
                    info.debug_port,
                    info.profile_id,
                    info.expires_at.isoformat() if info.expires_at else None,
                    str(info.artifacts_dir) if info.artifacts_dir else None,
                    now,
                    now,
                ),
            )

        logger.info("Created browser session record: %s", info.session_id)
        return info

    def get_browser_session(self, session_id: str) -> BrowserSessionInfo:
        """Get browser session info by session ID."""
        if not session_id:
            raise ValueError("session_id cannot be empty")

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM browser_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        if not row:
            raise BrowserSessionNotFoundError(session_id)

        return self._browser_session_info_from_row(row)

    def get_browser_session_config(self, session_id: str) -> BrowserSessionConfig:
        """Get the stored browser session config."""
        if not session_id:
            raise ValueError("session_id cannot be empty")

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT config FROM browser_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        if not row:
            raise BrowserSessionNotFoundError(session_id)

        return BrowserSessionConfig.model_validate_json(row["config"])

    def update_browser_session(
        self,
        session_id: str,
        *,
        status: BrowserSessionState | None = None,
        cdp_url: str | None = None,
        live_url: str | None = None,
        debug_port: int | None = None,
        profile_id: str | None = None,
        expires_at: datetime | None = None,
        artifacts_dir: Path | None = None,
        config: BrowserSessionConfig | None = None,
    ) -> BrowserSessionInfo:
        """Update a browser session record."""
        if not session_id:
            raise ValueError("session_id cannot be empty")

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection(exclusive=True) as conn:
            existing = conn.execute(
                "SELECT session_id FROM browser_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not existing:
                raise BrowserSessionNotFoundError(session_id)

            updates = ["updated_at = ?"]
            params: list[object] = [now]

            if status is not None:
                updates.append("status = ?")
                params.append(status.value)
            if cdp_url is not None:
                updates.append("cdp_url = ?")
                params.append(cdp_url)
            if live_url is not None:
                updates.append("live_url = ?")
                params.append(live_url)
            if debug_port is not None:
                updates.append("debug_port = ?")
                params.append(debug_port)
            if profile_id is not None:
                updates.append("profile_id = ?")
                params.append(profile_id)
            if expires_at is not None:
                updates.append("expires_at = ?")
                params.append(expires_at.isoformat())
            if artifacts_dir is not None:
                updates.append("artifacts_dir = ?")
                params.append(str(artifacts_dir))
            if config is not None:
                updates.append("config = ?")
                params.append(config.model_dump_json())

            params.append(session_id)
            conn.execute(
                f"UPDATE browser_sessions SET {', '.join(updates)} WHERE session_id = ?",
                params,
            )

        logger.info("Updated browser session: %s", session_id)
        return self.get_browser_session(session_id)

    def delete_browser_session(self, session_id: str) -> None:
        """Delete a browser session record."""
        if not session_id:
            raise ValueError("session_id cannot be empty")

        with self._get_connection(exclusive=True) as conn:
            result = conn.execute(
                "DELETE FROM browser_sessions WHERE session_id = ?",
                (session_id,),
            )
            if result.rowcount == 0:
                raise BrowserSessionNotFoundError(session_id)

        logger.info("Deleted browser session: %s", session_id)

    def list_browser_sessions(
        self, status: BrowserSessionState | None = None
    ) -> list[BrowserSessionInfo]:
        """List browser sessions, optionally filtered by status."""
        with self._get_connection() as conn:
            if status is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM browser_sessions
                    WHERE status = ?
                    ORDER BY created_at
                    """,
                    (status.value,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM browser_sessions ORDER BY created_at").fetchall()

        return [self._browser_session_info_from_row(row) for row in rows]

    def allocate_ip(
        self,
        vm_id: str,
        tap_device: str,
        requested_ip: str | None = None,
    ) -> str:
        """Atomically allocate the next available IP address.

        Args:
            vm_id: The VM to allocate for.
            tap_device: The TAP device name.
            requested_ip: Specific IP to reserve (optional).

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
            existing = conn.execute(
                "SELECT ip FROM ip_leases WHERE vm_id = ?",
                (vm_id,),
            ).fetchone()
            if existing:
                existing_ip = str(existing["ip"])
                if requested_ip and existing_ip != requested_ip:
                    raise NetworkError(
                        f"VM {vm_id} already has IP {existing_ip}, cannot reserve {requested_ip}"
                    )
                return existing_ip

            # Find all allocated IPs
            allocated = conn.execute("SELECT ip FROM ip_leases").fetchall()
            allocated_set = {row["ip"] for row in allocated}

            candidate_ips = [requested_ip] if requested_ip else [
                f"{IP_PREFIX}{i}" for i in range(IP_POOL_START, IP_POOL_END + 1)
            ]
            for ip in candidate_ips:
                if ip is None or ip in allocated_set:
                    continue
                conn.execute(
                    """
                    INSERT INTO ip_leases (ip, vm_id, tap_device, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ip, vm_id, tap_device, now),
                )
                logger.info("Allocated IP %s to VM %s", ip, vm_id)
                return ip

        if requested_ip:
            raise NetworkError(f"Requested IP address {requested_ip} is not available")
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

    def reserve_ssh_port(
        self,
        vm_id: str,
        guest_port: int = 22,
        host_port: int | None = None,
    ) -> int:
        """Reserve a host port for forwarding to guest SSH.

        Args:
            vm_id: The VM identifier.
            guest_port: Guest-side TCP port (default: 22).
            host_port: Specific host port to reserve (optional).

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
                existing_host_port = int(existing["host_port"])
                if host_port is not None and existing_host_port != host_port:
                    raise NetworkError(
                        f"VM {vm_id} already has SSH host port {existing_host_port}, "
                        f"cannot reserve {host_port}"
                    )
                return existing_host_port

            allocated = conn.execute("SELECT host_port FROM ssh_forwards").fetchall()
            allocated_set = {int(row["host_port"]) for row in allocated}

            candidate_ports = [host_port] if host_port is not None else range(
                SSH_PORT_START, SSH_PORT_END + 1
            )
            for candidate_port in candidate_ports:
                if candidate_port in allocated_set:
                    continue
                conn.execute(
                    """
                    INSERT INTO ssh_forwards (vm_id, host_port, guest_port, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (vm_id, candidate_port, guest_port, now),
                )
                logger.info("Reserved SSH host port %d for VM %s", candidate_port, vm_id)
                return candidate_port

        if host_port is not None:
            raise NetworkError(f"Requested SSH host port {host_port} is not available")
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
        """Check for stale VMs (marked RUNNING/PAUSED but process is dead).

        Returns:
            List of VM IDs that were marked as ERROR.
        """
        import os

        stale_vms = []

        with self._get_connection(exclusive=True) as conn:
            running = conn.execute(
                "SELECT id, pid FROM vms WHERE status IN (?, ?)",
                (VMState.RUNNING.value, VMState.PAUSED.value),
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
