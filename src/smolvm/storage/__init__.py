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

"""SmolVM storage backends.

Provides a :func:`create_state_manager` factory that returns the
appropriate backend based on configuration:

- **SQLite** (default): zero-config, local file at ``{data_dir}/smolvm.db``
- **PostgreSQL**: set ``SMOLVM_DATABASE_URL=postgresql://...`` for
  production fleet deployments with row-level locking and multi-host state

Example::

    from smolvm.storage import create_state_manager

    # SQLite (default)
    state = create_state_manager(db_path=data_dir / "smolvm.db")

    # PostgreSQL (via env var or explicit URL)
    state = create_state_manager(database_url="postgresql://user:pass@host/smolvm")
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from smolvm.storage._base import (
    IP_POOL_END,
    IP_POOL_START,
    IP_PREFIX,
    SSH_PORT_END,
    SSH_PORT_START,
)
from smolvm.storage._protocol import StateManagerProtocol
from smolvm.storage._sqlite import SQLiteStateManager

logger = logging.getLogger(__name__)

# Env var for configuring the database backend
DATABASE_URL_ENV = "SMOLVM_DATABASE_URL"


def create_state_manager(
    db_path: Path | None = None,
    database_url: str | None = None,
) -> StateManagerProtocol:
    """Create the appropriate state manager backend.

    Resolution order:

    1. Explicit *database_url* parameter
    2. ``SMOLVM_DATABASE_URL`` environment variable
    3. *db_path* parameter (SQLite)

    Args:
        db_path: Path to SQLite database file (default backend).
        database_url: PostgreSQL connection string. Takes precedence
            over *db_path* when set.

    Returns:
        A state manager instance implementing :class:`StateManagerProtocol`.

    Raises:
        ValueError: If neither *db_path* nor a database URL is provided.
        SmolVMError: If the PostgreSQL driver is not installed.
    """
    url = database_url or os.environ.get(DATABASE_URL_ENV)

    if url and (url.startswith("postgresql") or url.startswith("postgres://")):
        from smolvm.storage._postgres import PostgresStateManager

        logger.info("Using PostgreSQL state backend: %s", _redact_url(url))
        return PostgresStateManager(url)

    if db_path is not None:
        return SQLiteStateManager(db_path)

    raise ValueError(
        "No database configured. Provide db_path for SQLite or set "
        f"{DATABASE_URL_ENV} for PostgreSQL."
    )


def _redact_url(url: str) -> str:
    """Redact password from a database URL for logging."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if parsed.password:
        redacted = parsed._replace(
            netloc=f"{parsed.username}:***@{parsed.hostname}"
            + (f":{parsed.port}" if parsed.port else "")
        )
        return urlunparse(redacted)
    return url


# Backwards compatibility: re-export StateManager as alias for SQLiteStateManager.
# Consumers should migrate to create_state_manager() and StateManagerProtocol.
StateManager = SQLiteStateManager

__all__ = [
    "IP_POOL_END",
    "IP_POOL_START",
    "IP_PREFIX",
    "SSH_PORT_END",
    "SSH_PORT_START",
    "StateManager",
    "StateManagerProtocol",
    "SQLiteStateManager",
    "create_state_manager",
]
