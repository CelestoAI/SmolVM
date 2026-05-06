import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from smolvm.telemetry import BootStatus, CommandExecution, TelemetryManager, VMTelemetry


class TestTelemetryManager:
    """Test VM-level telemetry collection."""

    def test_telemetry_initialization(self, tmp_path: Path) -> None:
        """Test that TelemetryManager initializes correctly."""
        telemetry_path = tmp_path / "test.json"
        manager = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        assert manager.vm_id == "test-vm"
        assert manager.telemetry_path == telemetry_path

    def test_record_boot_start(self, tmp_path: Path) -> None:
        """Test recording boot start event."""
        telemetry_path = tmp_path / "test.json"
        manager = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        manager.record_boot_start()
        
        assert len(manager.get_data().boot_events) == 1
        assert manager.get_data().boot_events[0].status == BootStatus.STARTED

    def test_record_boot_ready(self, tmp_path: Path) -> None:
        """Test recording successful boot completion."""
        import time
        telemetry_path = tmp_path / "test.json"
        manager = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        manager.record_boot_start()
        time.sleep(0.1)  # Add 100ms delay
        manager.record_boot_ready()
        
        assert len(manager.get_data().boot_events) == 2
        assert manager.get_data().boot_events[1].status == BootStatus.READY
        assert manager.get_data().boot_events[1].duration_ms is not None
        assert manager.get_data().boot_events[1].duration_ms >= 100  # Should be ~100ms

    def test_record_boot_failure(self, tmp_path: Path) -> None:
        """Test recording boot failure with error message."""
        telemetry_path = tmp_path / "test.json"
        manager = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        error_msg = "SSH timeout after 30 seconds"
        manager.record_boot_start()
        manager.record_boot_failed(error_msg) 
        
        assert len(manager.get_data().boot_events) == 2
        assert manager.get_data().boot_events[1].status == BootStatus.FAILED
        assert manager.get_data().boot_events[1].error == error_msg

    def test_record_command_execution(self, tmp_path: Path) -> None:
        """Test recording SSH command execution."""
        telemetry_path = tmp_path / "test.json"
        manager = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        manager.record_command_execution(
            command="uname -a",
            exit_code=0,
            stdout="Linux test-vm 5.10.0 #1 SMP ...",
            stderr="",
            duration_ms=150,
        )
        
        assert len(manager.get_data().command_executions) == 1
        exec_data = manager.get_data().command_executions[0]
        assert exec_data.command == "uname -a"
        assert exec_data.exit_code == 0
        assert exec_data.duration_ms == 150

    def test_persistence_to_json(self, tmp_path: Path) -> None:
        """Test that telemetry is persisted to JSON file."""
        telemetry_path = tmp_path / "test.json"
        manager = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        manager.record_boot_start()
        manager.record_boot_ready()
        manager.record_command_execution(
            command="ls",
            exit_code=0,
            stdout="/bin /etc /usr ...",
            stderr="",
            duration_ms=100,
        )
        
        # Verify file exists and contains valid JSON
        assert telemetry_path.exists()
        with open(telemetry_path) as f:
            lines = [line.strip() for line in f if line.strip()]
            # JSON Lines format: one JSON object per line
            assert len(lines) >= 3
            for line in lines:
                data = json.loads(line)
                assert "vm_id" in data
                assert data["vm_id"] == "test-vm"

    def test_load_existing_telemetry(self, tmp_path: Path) -> None:
        """Test that TelemetryManager loads existing telemetry file."""
        telemetry_path = tmp_path / "test.json"
        
        # Create initial manager and record data
        manager1 = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        manager1.record_boot_start()
        manager1.record_boot_ready()
        
        # Create new manager for same file
        manager2 = TelemetryManager(vm_id="test-vm", telemetry_path=telemetry_path)
        
        # Should have loaded previous data
        data = manager2.get_data()
        assert len(data.boot_events) >= 2