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

"""High-level memory management for AI agents SmolVM."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from smolvm.memory._protocol import MemoryBackend
from smolvm.memory._types import MemoryFact, MemoryQuery, MemoryResult, MemoryEventType

logger = logging.getLogger(__name__)

class MemoryManager:
    """High-level memory manager for AI agents.

    Provides simple methods for agents to:
    - Record command executions
    - Store custom insights
    - Query memory semantically
    """

    def __init__(self, backend: MemoryBackend) -> None:
        self._backend = backend

    def record_command_execution(
        self,
        vm_id: str,
        command: str,
        stdout: str,
        stderr: str,
        return_code: int,
        duration_sec: float = 0.0,
    ) -> str:
        """Record a command execution to memory."""
        
        # Deduplicate: check if this exact command was recorded in last 10 seconds
        existing_facts = self._backend.list_facts(vm_id)
        for fact in existing_facts[-10:]:  # Check last 10 facts
            if (fact.metadata.get("command") == command and 
                fact.metadata.get("return_code") == return_code):
                # Same command with same result, skip duplicate
                return fact.fact_id
        
        stdout_preview = stdout[:500] if stdout else "(no output)"
        stderr_preview = stderr[:500] if stderr else "(no errors)"

        event_type = (
            MemoryEventType.COMMAND_EXECUTED
            if return_code == 0
            else MemoryEventType.COMMAND_FAILED
        )

        # Build fact content
        content = (
            f"Command: {command}\n"
            f"Exit Code: {return_code}\n"
            f"Duration: {duration_sec:.2f}s\n"
            f"Output: {stdout_preview}\n"
            f"Errors: {stderr_preview}"
        )

        fact = MemoryFact(
            fact_id=f"cmd-{uuid4().hex[:8]}",
            vm_id=vm_id,
            event_type=event_type,
            content=content,
            metadata={
                "command": command,
                "return_code": return_code,
                "duration_sec": duration_sec,
                "stdout": stdout,
                "stderr": stderr,
            },
            embedding=None,
            created_at=datetime.now(),
            retrieved_at=None,
        )
        
        self._backend.store_fact(fact)
        return fact.fact_id
    
    def record_user_note(
        self,
        vm_id: str,
        note: str,
        tags: list[str] | None = None,
    ) -> str:
        """Store a custom note or insight from the agent."""
        fact = MemoryFact(
            fact_id=f"note-{uuid4().hex[:8]}",
            vm_id=vm_id,
            event_type=MemoryEventType.USER_NOTE,
            content=note,
            metadata={
                "tags": tags or [],
            },
            embedding=None,
            created_at=datetime.now(),
            retrieved_at=None,
        )

        fact_id = self._backend.store_fact(fact)
        logger.debug(f"Recorded user note with {len(tags or [])} tags")
        return fact_id
    
    def record_pattern(
        self,
        vm_id: str,
        pattern: str,
        confidence: float = 1.0,
        context: dict | None = None,
    ) -> str:
        """Record a detected pattern or learned behavior."""

        fact = MemoryFact(
                fact_id=f"pattern-{uuid4().hex[:8]}",
                vm_id=vm_id,
                event_type=MemoryEventType.PATTERN_DETECTED,
                content=pattern,
                metadata={
                    "confidence": confidence,
                    "context": context or {},
                },
                embedding=None,
                created_at=datetime.now(),
                retrieved_at=None,
            )
        fact_id = self._backend.store_fact(fact)
        logger.debug(f"Recorded pattern detection with confidence {confidence:.2f}")
        return fact_id
    
    def recall_similar(
            self,
            query: str,
            vm_id: str | None = None,
            top_k: int = 5,
            similarity_threshold: float = 0.6,
    ) -> list[dict]:
        """Query memory for facts similar to the query.
        
        By default searches across all VMs (cross-agent memory sharing).
        Pass vm_id to restrict to a specific VM's facts.
        """

        memory_query = MemoryQuery(
            query_text=query,
            vm_id=vm_id,  # None = search all VMs
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )

        result: MemoryResult = self._backend.query_semantic(memory_query)

        recalled = [
            {
                "fact_id": fact.fact_id,
                "event_type": fact.event_type.value,
                "content": fact.content,
                "metadata": fact.metadata,
                "relevance": score,
                "created_at": fact.created_at.isoformat(),
            }
            for fact, score in zip(result.facts, result.relevance_scores)
        ]

        logger.debug(f"Semantic recall returned {len(recalled)} results")
        return recalled
    
    def list_vm_facts(self, vm_id: str) -> list[dict]:
        """List all facts for a specific VM."""

        facts = self._backend.list_facts(vm_id=vm_id)
        return [
            {
                "fact_id": fact.fact_id,
                "event_type": fact.event_type.value,
                "content": fact.content,
                "metadata": fact.metadata,
                "created_at": fact.created_at.isoformat(),
            }
            for fact in facts
        ]
    
    def delete_fact(self, fact_id: str) -> None:
        """Delete a specific fact from memory."""

        self._backend.delete_fact(fact_id)
        logger.debug(f"Deleted fact with ID {fact_id}")

    def close(self) -> None:
        """Cleanup memory resources."""
        
        self._backend.close()