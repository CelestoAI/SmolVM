# Copyright 2026 Celesto AI
"""Regression tests for the SDK/CLI persistence boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from smolvm.cli.state import create_cli_state_manager
from smolvm.storage import MemoryStateManager


def _run_isolated(script: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SMOLVM_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_sdk_uses_memory_state_without_importing_sqlite(tmp_path: Path) -> None:
    result = _run_isolated(
        """
import sys
from pathlib import Path
from smolvm.vm import SmolVMManager
manager = SmolVMManager(backend='qemu')
assert type(manager.state).__name__ == 'MemoryStateManager'
assert not (manager.data_dir / 'smolvm.db').exists()
assert 'smolvm.cli._sqlite' not in sys.modules
manager.close()
print('ok')
""",
        tmp_path / "sdk",
    )
    assert result.stdout.strip() == "ok"


def test_http_api_does_not_import_cli_sqlite(tmp_path: Path) -> None:
    result = _run_isolated(
        """
import sys
from pathlib import Path
from smolvm.server.app import create_app
create_app()
assert not (Path(__import__('os').environ['SMOLVM_DATA_DIR']) / 'smolvm.db').exists()
assert 'smolvm.cli._sqlite' not in sys.modules
print('ok')
""",
        tmp_path / "api",
    )
    assert result.stdout.strip() == "ok"


def test_cli_state_keeps_existing_database_location(tmp_path: Path) -> None:
    db_path = tmp_path / "smolvm.db"
    state = create_cli_state_manager(db_path)
    try:
        assert db_path.exists()
    finally:
        state.close()


def test_transient_resource_claims_coordinate_process_local_managers(tmp_path: Path) -> None:
    first = MemoryStateManager(tmp_path)
    second = MemoryStateManager(tmp_path)

    assert first.reserve_ssh_port("one") == 2200
    assert second.reserve_ssh_port("two") == 2201

    first.release_ssh_port("one")
    assert second.reserve_ssh_port("three") == 2200
