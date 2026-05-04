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

"""Data types for SmolVM memory layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

class MemoryEventType(str, Enum):
    """Types of events stored in memory."""

    COMMAND_EXECUTED = "command_executed"
    COMMAND_FAILED = "command_failed"
    VM_CREATED = "vm_created"
    VM_STARTED = "vm_started"
    PATTERN_DETECTED = "pattern_detected"
    USER_NOTE = "user_note"
    CUSTOM = "custom"

@dataclass(slots=True, frozen=True)
class MemoryFact:
    """A single fact stored in memory."""

    fact_id: str
    """Unique identifier for this fact."""

    vm_id: str
    """ID of the VM this fact is associated with."""

    event_type: MemoryEventType
    """Type of event (what happened?)."""

    content: str
    """Text description of the fact (will be embedded for semantic search)."""

    metadata: dict[str, Any]
    """Additional data (command, duration, return code, etc.)."""

    embedding: list[float] | None
    """Vector embedding for semantic search (computed on store)."""

    created_at: datetime
    """When this fact was created."""

    retrieved_at: datetime | None
    """Last time this fact was recalled by an agent."""

@dataclass(slots=True, frozen=True)
class MemoryQuery:
    """Query object for semantic search."""

    query_text: str
    """The search query text (will be embedded for comparison)."""

    vm_id: str | None = None
    """Optional: scope search to a specific VM."""

    event_type: MemoryEventType | None = None
    """Optional: filter by event type."""

    top_k: int = 5
    """How many results to return."""

    similarity_threshold: float = 0.6
    """Minimum cosine similarity for results."""

@dataclass(slots=True, frozen=True)
class MemoryResult:
    """Result from semantic memory search."""

    facts: list[MemoryFact]
    """Ranked facts matching the query."""

    relevance_scores: list[float]
    """Cosine similarity scores for each fact."""

    query_embedding: list[float]
    """Echo back the query embedding."""