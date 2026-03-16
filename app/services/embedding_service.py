"""Embedding service for math problem similarity search.

Key innovation: Separate embeddings for question and solution text.
- question_vector: embed(question + refer + choices) — problem structure
- solution_vector: embed(solution) — solving method (weighted higher)
"""
import asyncio
import numpy as np
from openai import AsyncOpenAI, RateLimitError
from app.config import OPENAI_API_KEY_EMBEDDING


def embedding_to_bytes(embedding: list[float]) -> bytes:
    """Convert embedding list to numpy float32 bytes for BLOB storage."""
    return np.array(embedding, dtype=np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    """Convert BLOB bytes back to numpy float32 array."""
    return np.frombuffer(data, dtype=np.float32)

EMB_MODEL = "text-embedding-3-large"
EMB_DIM = 3072

class EmbeddingService:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY_EMBEDDING)

    async def embed_text(self, text: str) -> list[float]:
        """Single text → 3072d vector."""
        resp = await self.client.embeddings.create(model=EMB_MODEL, input=text)
        return resp.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed multiple texts. Handles rate limits."""
        # Implement with chunking (max ~8000 tokens per batch call)
        # Split into sub-batches if needed
        results = []
        batch_size = 50  # safe batch size
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            try:
                resp = await self.client.embeddings.create(model=EMB_MODEL, input=batch)
                results.extend([d.embedding for d in resp.data])
            except RateLimitError:
                await asyncio.sleep(5)
                resp = await self.client.embeddings.create(model=EMB_MODEL, input=batch)
                results.extend([d.embedding for d in resp.data])
        return results

    async def embed_problem_split(self, question_text: str, solution_text: str) -> dict:
        """Generate separate embeddings for question and solution."""
        q_vec, s_vec = await asyncio.gather(
            self.embed_text(question_text),
            self.embed_text(solution_text),
        )
        return {"question_vector": q_vec, "solution_vector": s_vec}

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        a_np = np.array(a, dtype=np.float32)
        b_np = np.array(b, dtype=np.float32)
        return float(np.dot(a_np, b_np) / (np.linalg.norm(a_np) * np.linalg.norm(b_np)))

    @staticmethod
    def weighted_similarity(
        q_vec_a: list[float], q_vec_b: list[float],
        s_vec_a: list[float], s_vec_b: list[float],
        q_weight: float = 0.3, s_weight: float = 0.7,
    ) -> float:
        """Weighted combination of question and solution similarity.
        Default: solution gets 0.7 weight (풀이 방법이 주 신호)."""
        q_sim = EmbeddingService.cosine_similarity(q_vec_a, q_vec_b)
        s_sim = EmbeddingService.cosine_similarity(s_vec_a, s_vec_b)
        return q_weight * q_sim + s_weight * s_sim
