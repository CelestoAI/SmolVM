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

"""Protocol for memory backends in SmolVM."""

from typing import Protocol

from smolvm.memory._types import MemoryFact, MemoryQuery, MemoryResult

class MemoryBackend(Protocol):
    """Interface for AI agent memory storage backends."""

    def store_fact(self, fact: MemoryFact) -> str:
        """Store a fact in memory and return its unique ID."""
        ...
    
    def query_semantic(self, query: MemoryQuery) -> MemoryResult:
        """Search memory by semantic similarity."""
        ...
    
    def delete_fact(self, fact_id: str) -> None:
        """Remove a fact from memory."""
        ...
    
    def close(self) -> None:
        """Cleanup resources."""
        ...