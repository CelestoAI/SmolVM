# Copyright 2026 Celesto AI
"""Regression tests for the SDK/CLI persistence boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli.service import CLIService
from smolvm.cli.state import create_cli_state_manager
from smolvm.storage import MemoryStateManager


def _run_isolated(script: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SMOLVM_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result


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
    pytest.importorskip("fastapi")
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


def test_memory_state_requires_a_secure_lock_directory() -> None:
    with pytest.raises(ValueError, match="data_dir is required"):
        MemoryStateManager()


def test_cli_service_reuses_one_inventory(tmp_path: Path) -> None:
    state_manager = MagicMock()
    with patch(
        "smolvm.cli.state.create_cli_state_manager", return_value=state_manager
    ) as create_state:
        service = CLIService(tmp_path)

    assert service.state_manager() is state_manager
    assert service.state_manager() is state_manager
    create_state.assert_called_once_with(tmp_path / "smolvm.db")


def test_transient_resource_claims_coordinate_process_local_managers(tmp_path: Path) -> None:
    first = MemoryStateManager(tmp_path)
    second = MemoryStateManager(tmp_path)

    assert first.reserve_ssh_port("one") == 2200
    assert second.reserve_ssh_port("two") == 2201

    first.release_ssh_port("one")
    assert second.reserve_ssh_port("three") == 2200
