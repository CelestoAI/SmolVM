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

"""Embedding generation and semantic similarity scoring."""

from __future__ import annotations

import math


class SearchSimilarity:
    """Utility class for computing vector similarity."""

    @staticmethod
    def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
        """Compute cosine similarity between two embedding vectors.
        
        Args:
            vec1: First embedding vector.
            vec2: Second embedding vector.
            
        Returns:
            Cosine similarity score in range [-1, 1].
        """
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)


class SentenceTransformerEmbedder:
    """Embedding provider using sentence-transformers library.
    
    Converts text to semantic embeddings using pretrained language models.
    """

    def __init__(self, model: str = "all-minilm-l6-v2") -> None:
        """Initialize the embedding model.
        
        Args:
            model: Hugging Face model identifier for sentence-transformers.
                   Defaults to 'all-minilm-l6-v2' (lightweight, 384-dim).
                   
        Raises:
            ImportError: If sentence-transformers is not installed.
        """
        try:
            import warnings
            from sentence_transformers import SentenceTransformer

            # Suppress the "group argument" warning from transformers library
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*group argument must be None.*")
                warnings.filterwarnings("ignore", category=UserWarning)
                self.model = SentenceTransformer(model)
        except ImportError as e:
            raise ImportError(
                "Memory layer requires 'sentence-transformers'. "
                "Install with: pip install 'smolvm[memory]' or "
                "pip install sentence-transformers"
            ) from e

    def embed_text(self, text: str) -> list[float]:
        """Convert text to embedding vector.
        
        Args:
            text: Text to embed.
            
        Returns:
            Embedding vector as list of floats.
        """
        embedding = self.model.encode(text, convert_to_tensor=False)
        return embedding.tolist()


# Alias for backwards compatibility
EmbeddingIndexer = SentenceTransformerEmbedder