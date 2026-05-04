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

"""Tests for SmolVM memory layer."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.memory._sqlite import SQLiteMemoryBackend
from smolvm.memory._types import MemoryEventType, MemoryFact, MemoryQuery
from smolvm.memory.indexer import SearchSimilarity, SentenceTransformerEmbedder
from smolvm.memory.manager import MemoryManager


class MockEmbedder:
    """Mock embedding provider for deterministic tests."""

    def embed_text(self, text: str) -> list[float]:
        """Return deterministic embedding based on text hash."""
        # Simple: return a vector based on text length + hash
        hash_val = hash(text) & 0x7FFFFFFF  # Positive only
        base = [float(hash_val % 256) / 256.0] * 384
        # Make it normalized for cosine similarity
        norm = sum(x * x for x in base) ** 0.5
        return [x / (norm + 1e-8) for x in base]

    @staticmethod
    def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        return SearchSimilarity.cosine_similarity(vec1, vec2)


class TestSearchSimilarity:
    """Tests for vector similarity computation."""

    def test_cosine_similarity_identical_vectors(self) -> None:
        """Identical vectors should have similarity 1.0."""
        vec = [1.0, 0.0, 0.0]
        assert SearchSimilarity.cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal_vectors(self) -> None:
        """Orthogonal vectors should have similarity 0.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]
        assert SearchSimilarity.cosine_similarity(vec1, vec2) == pytest.approx(0.0)

    def test_cosine_similarity_zero_vector(self) -> None:
        """Zero vector similarity should be 0.0."""
        vec1 = [0.0, 0.0, 0.0]
        vec2 = [1.0, 1.0, 1.0]
        assert SearchSimilarity.cosine_similarity(vec1, vec2) == pytest.approx(0.0)

    def test_cosine_similarity_opposite_vectors(self) -> None:
        """Opposite vectors should have negative similarity."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [-1.0, 0.0, 0.0]
        result = SearchSimilarity.cosine_similarity(vec1, vec2)
        assert result == pytest.approx(-1.0)


class TestSQLiteMemoryBackend:
    """Tests for SQLite memory backend."""

    @pytest.fixture
    def temp_db(self) -> Path:
        """Create temporary SQLite database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test.db"

    @pytest.fixture
    def backend(self, temp_db: Path) -> SQLiteMemoryBackend:
        """Create SQLite backend with mock embedder."""
        with patch("smolvm.memory._sqlite.EmbeddingIndexer", return_value=MockEmbedder()):
            backend = SQLiteMemoryBackend(temp_db)
        return backend

    def test_store_and_retrieve_fact(self, backend: SQLiteMemoryBackend) -> None:
        """Store a fact and retrieve it."""
        fact = MemoryFact(
            fact_id="test-1",
            vm_id="vm-001",
            event_type=MemoryEventType.COMMAND_EXECUTED,
            content="npm install completed successfully",
            metadata={"command": "npm install", "return_code": 0, "duration": 15.5},
            embedding=None,
            created_at=datetime.now(),
            retrieved_at=None,
        )

        fact_id = backend.store_fact(fact)
        assert fact_id == "test-1"

        # Retrieve it
        facts = backend.list_facts(vm_id="vm-001")
        assert len(facts) == 1
        assert facts[0].fact_id == "test-1"
        assert facts[0].content == "npm install completed successfully"
        assert facts[0].metadata["return_code"] == 0

    def test_semantic_query(self, backend: SQLiteMemoryBackend) -> None:
        """Test semantic search functionality."""
        # Store facts
        facts_data = [
            ("npm install succeeded", MemoryEventType.COMMAND_EXECUTED, 0),
            ("npm install failed with timeout", MemoryEventType.COMMAND_FAILED, 1),
            ("docker run crashed", MemoryEventType.COMMAND_FAILED, 1),
        ]

        for i, (content, event_type, return_code) in enumerate(facts_data):
            fact = MemoryFact(
                fact_id=f"fact-{i}",
                vm_id="vm-001",
                event_type=event_type,
                content=content,
                metadata={"return_code": return_code},
                embedding=None,
                created_at=datetime.now(),
                retrieved_at=None,
            )
            backend.store_fact(fact)

        # Query for npm-related issues
        query = MemoryQuery(
            query_text="npm install problems",
            vm_id="vm-001",
            top_k=2,
            similarity_threshold=0.0,  # Accept all for testing
        )

        result = backend.query_semantic(query)
        assert len(result.facts) <= 2
        assert len(result.relevance_scores) == len(result.facts)

    def test_list_facts_by_vm(self, backend: SQLiteMemoryBackend) -> None:
        """List facts filtered by VM ID."""
        for i, vm_id in enumerate(["vm-001", "vm-001", "vm-002"]):
            fact = MemoryFact(
                fact_id=f"fact-{i}",
                vm_id=vm_id,
                event_type=MemoryEventType.USER_NOTE,
                content=f"Note for {vm_id}",
                metadata={},
                embedding=None,
                created_at=datetime.now(),
                retrieved_at=None,
            )
            backend.store_fact(fact)

        # List only vm-001 facts
        vm1_facts = backend.list_facts(vm_id="vm-001")
        assert len(vm1_facts) == 2

        # List all facts
        all_facts = backend.list_facts()
        assert len(all_facts) == 3

    def test_delete_fact(self, backend: SQLiteMemoryBackend) -> None:
        """Delete a fact from backend."""
        fact = MemoryFact(
            fact_id="test-delete",
            vm_id="vm-001",
            event_type=MemoryEventType.USER_NOTE,
            content="This will be deleted",
            metadata={},
            embedding=None,
            created_at=datetime.now(),
            retrieved_at=None,
        )

        backend.store_fact(fact)
        assert len(backend.list_facts(vm_id="vm-001")) == 1

        backend.delete_fact("test-delete")
        assert len(backend.list_facts(vm_id="vm-001")) == 0


class TestMemoryManager:
    """Tests for high-level memory manager."""

    @pytest.fixture
    def temp_db(self) -> Path:
        """Create temporary SQLite database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test.db"

    @pytest.fixture
    def manager(self, temp_db: Path) -> MemoryManager:
        """Create memory manager with mock backend."""
        with patch("smolvm.memory._sqlite.EmbeddingIndexer", return_value=MockEmbedder()):
            backend = SQLiteMemoryBackend(temp_db)
        return MemoryManager(backend)

    def test_record_command_execution_success(self, manager: MemoryManager) -> None:
        """Record successful command execution."""
        fact_id = manager.record_command_execution(
            vm_id="vm-001",
            command="ls /",
            stdout="bin\ndev\netc",
            stderr="",
            return_code=0,
            duration_sec=0.05,
        )

        assert fact_id.startswith("cmd-")

        # Verify stored
        facts = manager.list_vm_facts("vm-001")
        assert len(facts) == 1
        assert facts[0]["event_type"] == "command_executed"
        assert facts[0]["metadata"]["return_code"] == 0

    def test_record_command_execution_failure(self, manager: MemoryManager) -> None:
        """Record failed command execution."""
        fact_id = manager.record_command_execution(
            vm_id="vm-001",
            command="npm install",
            stdout="",
            stderr="ERR! not ok",
            return_code=1,
            duration_sec=45.0,
        )

        facts = manager.list_vm_facts("vm-001")
        assert facts[0]["event_type"] == "command_failed"
        assert facts[0]["metadata"]["return_code"] == 1
        assert facts[0]["metadata"]["duration_sec"] == 45.0

    def test_record_user_note(self, manager: MemoryManager) -> None:
        """Store custom user note."""
        fact_id = manager.record_user_note(
            vm_id="vm-001",
            note="Learned that npm needs 4GB for large monorepos",
            tags=["npm", "memory"],
        )

        assert fact_id.startswith("note-")

        facts = manager.list_vm_facts("vm-001")
        assert facts[0]["event_type"] == "user_note"
        assert "npm" in facts[0]["metadata"]["tags"]

    def test_record_pattern(self, manager: MemoryManager) -> None:
        """Record detected pattern."""
        fact_id = manager.record_pattern(
            vm_id="vm-001",
            pattern="Docker builds fail when disk < 5GB",
            confidence=0.95,
            context={"min_disk_gb": 5},
        )

        assert fact_id.startswith("pattern-")

        facts = manager.list_vm_facts("vm-001")
        assert facts[0]["event_type"] == "pattern_detected"
        assert facts[0]["metadata"]["confidence"] == 0.95

    def test_recall_similar(self, manager: MemoryManager) -> None:
        """Query memory for similar facts."""
        # Store several facts
        manager.record_command_execution(
            vm_id="vm-001",
            command="npm install",
            stdout="",
            stderr="timeout",
            return_code=1,
            duration_sec=120.0,
        )

        manager.record_user_note(
            vm_id="vm-001",
            note="npm install takes too long with 2GB memory",
            tags=["npm"],
        )

        # Query
        results = manager.recall_similar(
            "npm install timeout",
            vm_id="vm-001",
            top_k=5,
            similarity_threshold=0.0,  # Accept all
        )

        assert len(results) > 0
        assert all("relevance" in r for r in results)

    def test_close(self, manager: MemoryManager) -> None:
        """Test cleanup."""
        manager.close()  # Should not raise


class TestMemoryIntegration:
    """Integration tests with SmolVM facade."""

    @pytest.mark.skip(reason="Requires full SmolVM setup and working VM")
    def test_vm_memory_auto_record(self) -> None:
        """Test that vm.run() auto-records to memory."""
        # This would require actual SmolVM setup
        # Kept as a placeholder for integration testing
        pass

    @pytest.mark.skip(reason="Requires full SmolVM setup")
    def test_vm_remember_recall(self) -> None:
        """Test agent recall of similar past experience."""
        pass

    @pytest.mark.skip(reason="Requires full SmolVM setup")
    def test_vm_note_storage(self) -> None:
        """Test agent note storage."""
        pass
