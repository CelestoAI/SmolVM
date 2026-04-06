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

"""Shared helpers and constants for SmolVM storage backends."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smolvm.types import (
    BrowserSessionInfo,
    BrowserSessionState,
    NetworkConfig,
    SnapshotArtifacts,
    SnapshotInfo,
    VMConfig,
)

# IP allocation pool: 172.16.0.2 - 172.16.0.254
IP_POOL_START = 2
IP_POOL_END = 254
IP_PREFIX = "172.16.0."

# SSH host-port forwarding pool: 2200 - 2999
SSH_PORT_START = 2200
SSH_PORT_END = 2999


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def vm_config_from_json(raw: str) -> VMConfig:
    """Deserialize VM config while trusting persisted filesystem paths."""
    return VMConfig.model_validate_json(raw, context={"validate_paths": False})


def snapshot_info_from_row(row: Any) -> SnapshotInfo:
    """Convert a database row (dict-like) into a SnapshotInfo."""
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
        vm_config=vm_config_from_json(row["vm_config"]),
        network_config=NetworkConfig.model_validate_json(row["network_config"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        restored=bool(row["restored"]),
        restored_vm_id=row["restored_vm_id"],
    )


def browser_session_info_from_row(row: Any) -> BrowserSessionInfo:
    """Convert a database row (dict-like) into a BrowserSessionInfo."""
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
