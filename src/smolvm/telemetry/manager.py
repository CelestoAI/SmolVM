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

"""VM telemetry recording and management for SmolVM."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from smolvm.telemetry._types import BootEvent, BootStatus, CommandExecution, VMTelemetry

logger = logging.getLogger(__name__)

class TelemetryManager:
    """Records and persists VM telemetry to JSON file."""

    def __init__(self, vm_id: str, telemetry_path: Path) -> None:
        self.vm_id = vm_id
        self.telemetry_path = telemetry_path
        self._data : VMTelemetry = VMTelemetry(vm_id=vm_id, created_at=datetime.now())
        self._boot_start_time : float | None = None

        # Load existing telemetry if file exists
        if self.telemetry_path.exists():
            try:
                with open(self.telemetry_path, "r") as f:
                    content = f.read().strip()
                    if content:
                        # JSON Lines format: load last non-empty line
                        lines = [line for line in content.split("\n") if line.strip()]
                        if lines:
                            last_entry = json.loads(lines[-1])
                            loaded = VMTelemetry(**last_entry)
                            self._data = loaded
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load telemetry from {self.telemetry_path}: {e}")
    
    def record_boot_start(self) -> None:
        """Record boot start timestamp."""
        self._boot_start_time = datetime.now().timestamp()
        event = BootEvent(
            timestamp=datetime.now(),
            status=BootStatus.STARTED,
        )
        self._data.boot_events.append(event)
        self._persist()
    
    def record_boot_ready(self) -> None:
        """Record successful boot completion."""
        
        if self._boot_start_time is None:
            logger.warning("record_boot_ready called without prior record_boot_start")
            self._boot_start_time = datetime.now().timestamp()  # Fallback to current time

        duration_ms = int((datetime.now().timestamp() - self._boot_start_time) * 1000)
        event = BootEvent(
            timestamp=datetime.now(),
            status=BootStatus.STARTED,
        )
        self._data.boot_events.append(event)
        self._persist()
    
    def record_boot_ready(self) -> None:
        """Record successful boot completion."""

        if self._boot_start_time is None:
            logger.warning("record_boot_ready called without prior record_boot_start")
            self._boot_start_time = datetime.now().timestamp()  # Fallback to current time

        duration_ms = int((datetime.now().timestamp() - self._boot_start_time) * 1000)
        event = BootEvent(
            timestamp=datetime.now(),
            status=BootStatus.READY,
            duration_ms=duration_ms,
        )
        self._data.boot_events.append(event)
        self._persist()
    
    def record_boot_failed(self, error: str) -> None:
        """Record boot failure with error message."""
        if self._boot_start_time is None:
            logger.warning("record_boot_failed called without prior record_boot_start")
            self._boot_start_time = datetime.now().timestamp()  # Fallback to current time

        duration_ms = int((datetime.now().timestamp() - self._boot_start_time) * 1000)
        event = BootEvent(
            timestamp=datetime.now(),
            status=BootStatus.FAILED,
            duration_ms=duration_ms,
            error=error,
        )
        self._data.boot_events.append(event)
        self._persist()
    
    def record_command_execution(
        self,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
    ) -> None:
        """Record SSH command execution."""

        execution = CommandExecution(
            timestamp=datetime.now(),
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )
        self._data.command_executions.append(execution)
        self._persist()
    
    def _persist(self) -> None:
        """Write current telemetry to JSON file (JSON Lines format)."""
        try:
            self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.telemetry_path, "a") as f:
                f.write(self._data.model_dump_json() + "\n")
        except OSError as e:
            logger.error(f"Failed to persist telemetry to {self.telemetry_path}: {e}")
        
    def get_data(self) -> VMTelemetry:
        """Get current telemetry data."""
        return self._data
    
    def clear(self) -> None:
        """Clear telemetry data (called on VM delete)."""
        self._data = VMTelemetry(vm_id=self.vm_id, created_at=datetime.now())
        # Don't delete file, just clear in-memory state