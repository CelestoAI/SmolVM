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

"""SQLite-based memory backend with vector embeddings."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from smolvm.memory._protocol import MemoryBackend
from smolvm.memory._types import MemoryFact, MemoryQuery, MemoryResult, MemoryEventType
from smolvm.memory.indexer import EmbeddingIndexer, SearchSimilarity

logger = logging.getLogger(__name__)

class SQLiteMemoryBackend(MemoryBackend):
    """SQLite-based memory backend with vector embeddings."""

    def __init__(self, db_path: Path | str) -> None:
        """Initialize the SQLite memory backend."""
        import warnings
        
        self.db_path = Path(db_path)
        
        # Suppress known transformers warnings during indexer initialization
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*group argument must be None.*")
            warnings.filterwarnings("ignore", category=UserWarning)
            self.indexer = EmbeddingIndexer()
        
        self._init_schema()
        logger.info(f"Initialized SQLiteMemoryBackend with DB at {self.db_path}")

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_facts (
                    fact_id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata JSON,
                    embedding JSON,
                    created_at TEXT NOT NULL,
                    retrieved_at TEXT
                    );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_vm_id
                ON memory_facts (vm_id);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_event_type
                ON memory_facts (event_type);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_created_at
                ON memory_facts (created_at);
                """
            )
            conn.commit()

    def store_fact(self, fact: MemoryFact) -> str:
        """Store a fact with embedding"""
        embedding = self.indexer.embed_text(fact.content)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_facts (
                    fact_id, vm_id, event_type, content, 
                    metadata, embedding, created_at, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
             (
                    fact.fact_id,
                    fact.vm_id,
                    fact.event_type.value,
                    fact.content,
                    json.dumps(fact.metadata),
                    json.dumps(embedding),
                    fact.created_at.isoformat(),
                    fact.retrieved_at.isoformat() if fact.retrieved_at else None,
                ),
            )
            conn.commit()

        logger.debug(f"Stored fact {fact.fact_id} (type={fact.event_type.value})")
        return fact.fact_id

    def query_semantic(self, query: MemoryQuery) -> MemoryResult:
        """Search facts by semantic similarity."""
        
        query_embedding = self.indexer.embed_text(query.query_text)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            where_clauses = ["1=1"]
            params: list = []

            if query.vm_id:
                where_clauses.append("vm_id = ?")
                params.append(query.vm_id)
            
            if query.event_type:
                where_clauses.append("event_type = ?")
                params.append(query.event_type.value)

            where_sql = " AND ".join(where_clauses)
            rows = conn.execute(
                f"SELECT * FROM memory_facts WHERE {where_sql} ORDER BY created_at DESC",
                params,
            ).fetchall()

        scored_facts: list[tuple[MemoryFact, float]] = []
        for row in rows:
            embedding = json.loads(row["embedding"])
            score = SearchSimilarity.cosine_similarity(query_embedding, embedding)

            if score >= query.similarity_threshold:
                fact = MemoryFact(
                    fact_id=row["fact_id"],
                    vm_id=row["vm_id"],
                    event_type=MemoryEventType(row["event_type"]),
                    content=row["content"],
                    metadata=json.loads(row["metadata"]),
                    embedding=embedding,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    retrieved_at=(
                        datetime.fromisoformat(row["retrieved_at"])
                        if row["retrieved_at"]
                        else None
                    ),
                )
                scored_facts.append((fact, score))

        scored_facts.sort(key=lambda x: x[1], reverse=True)
        top_facts = scored_facts[: query.top_k]

        facts = [f for f, _ in top_facts]
        scores = [s for _, s in top_facts]

        logger.debug(
                f"Semantic query returned {len(facts)} results "
                f"(threshold={query.similarity_threshold})"
        )   

        return MemoryResult(
                facts=facts,
                relevance_scores=scores,
                query_embedding=query_embedding,
        )
        
    def list_facts(self, vm_id: str | None = None) -> list[MemoryFact]:
        """List all facts, optionally filtered by VM."""

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if vm_id:
                rows = conn.execute(
                    "SELECT * FROM memory_facts WHERE vm_id = ? ORDER BY created_at DESC",
                    (vm_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_facts ORDER BY created_at DESC"
                ).fetchall()

        facts: list[MemoryFact] = []
        for row in rows:
            fact = MemoryFact(
                fact_id=row["fact_id"],
                vm_id=row["vm_id"],
                event_type=MemoryEventType(row["event_type"]),
                content=row["content"],
                metadata=json.loads(row["metadata"]),
                embedding=json.loads(row["embedding"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                retrieved_at=(
                    datetime.fromisoformat(row["retrieved_at"])
                    if row["retrieved_at"]
                    else None
                ),
            )
            facts.append(fact)

        return facts
    
    def delete_fact(self, fact_id: str) -> None:
        """Remove a fact from memory."""

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM memory_facts WHERE fact_id = ?", (fact_id,))
            conn.commit()

        logger.debug(f"Deleted fact {fact_id}")

    def close(self) -> None:
        """Cleanup resources."""
        logger.debug("SQLiteMemoryBackend closed.")