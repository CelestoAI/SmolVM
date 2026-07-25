# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""CLI-owned persistent inventory.

The Python SDK and HTTP API use process-local state. Only CLI surfaces open
``smolvm.db`` so separate command invocations can discover and reconnect to
sandboxes.
"""

from __future__ import annotations

from pathlib import Path

from smolvm.cli._sqlite import SQLiteStateManager
from smolvm.storage._protocol import StateManagerProtocol


def create_cli_state_manager(db_path: Path) -> StateManagerProtocol:
    """Open the CLI inventory at *db_path*."""
    return SQLiteStateManager(db_path)


__all__ = ["SQLiteStateManager", "create_cli_state_manager"]
