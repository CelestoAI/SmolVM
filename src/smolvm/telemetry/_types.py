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

"""Telemetry data types for SmolVM."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

class BootStatus(str, Enum):
    """Boot status of SmolVM."""

    STARTED = "started"
    READY = "ready"
    FAILED = "failed"

class BootEvent(BaseModel):
    """Record of a VM boot attempt."""

    timestamp: datetime
    status: BootStatus
    duration_ms: int | None = None
    error: str | None = None

    model_config = {"frozen": True}

class CommandExecution(BaseModel):
    """Record of an SSH command execution."""

    timestamp: datetime
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    model_config = {"frozen": True}

class VMTelemetry(BaseModel):
    """Telemetry snapshot for a VM."""

    vm_id: str
    created_at: datetime
    boot_events: list[BootEvent] = Field(default_factory=list)
    command_executions: list[CommandExecution] = Field(default_factory=list)

    model_config = {"frozen": False}