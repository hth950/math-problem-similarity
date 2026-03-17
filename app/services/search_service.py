"""Search service for math problem similarity.

Supports two modes:
1. Legacy: Single combined vector (question + solution together)
2. Improved: Split vectors with weighted combination (solution 0.7, question 0.3)
"""
import asyncio
import sqlite3
import os
import numpy as np
from pathlib import Path
from app.services.embedding_service import EmbeddingService

DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "math_problems.db"


class SearchService:
    def __init__(self):
        self.embedding_service = EmbeddingService()
        self._problems: list[dict] = []
        self._loaded = False

    def load_problems(self, path: str | None = None):
        """Load problem data with pre-computed vectors from SQLite."""
        if path is None:
            path = str(DEFAULT_DB_PATH)

        if not os.path.exists(path):
            self._problems = []
            self._loaded = True
            return

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("SELECT * FROM problems")
            rows = cursor.fetchall()
        finally:
            conn.close()

        problems = []
        for row in rows:
            prob = dict(row)
            # Convert BLOB vectors back to numpy arrays
            for vec_col in ("full_text_vector", "question_vector", "solution_vector"):
                blob = prob.get(vec_col)
                if blob is not None:
                    prob[vec_col] = np.frombuffer(blob, dtype=np.float32)
                else:
                    prob[vec_col] = None
            problems.append(prob)

        self._problems = problems
        self._loaded = True

    @staticmethod
    def _cosine_similarity_vectorized(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        """Compute cosine similarity between a query vector and a matrix of row vectors."""
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return np.zeros(len(matrix))
        norms = np.linalg.norm(matrix, axis=1)
        norms = np.where(norms == 0, 1e-10, norms)
        return (matrix @ query_vec) / (norms * query_norm)

    async def search_legacy(
        self, query_text: str, top_k: int = 10,
        grade: int | None = None, school_level: str | None = None,
        exclude_id: int | None = None,
    ) -> list[dict]:
        """Legacy search: embed full text (question+solution combined) and find similar."""
        if not self._loaded:
            self.load_problems()

        query_emb = await self.embedding_service.embed_text(query_text)
        query_vec = np.array(query_emb, dtype=np.float32)

        # Filter candidates
        candidates = [
            p for p in self._problems
            if p.get("full_text_vector") is not None
            and (exclude_id is None or p.get("id") != exclude_id)
            and (grade is None or str(p.get("grade")) == str(grade))
            and (school_level is None or p.get("school_level") == school_level)
        ]

        if not candidates:
            return []

        matrix = np.stack([p["full_text_vector"] for p in candidates])
        scores = self._cosine_similarity_vectorized(query_vec, matrix)

        # Get top_k indices
        top_indices = np.argpartition(scores, -min(top_k, len(scores)))[-top_k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            prob = {k: v for k, v in candidates[idx].items()
                    if k not in ("full_text_vector", "question_vector", "solution_vector")}
            prob["score"] = round(float(scores[idx]), 4)
            prob["search_type"] = "legacy"
            results.append(prob)

        return results

    async def search_improved(
        self, question_text: str, solution_text: str, top_k: int = 10,
        q_weight: float = 0.3, s_weight: float = 0.7,
        grade: int | None = None, school_level: str | None = None,
        exclude_id: int | None = None,
    ) -> list[dict]:
        """Improved search: separate question and solution embeddings with weighted combination."""
        if not self._loaded:
            self.load_problems()

        # Fallback: if no solution text, use question-only with full weight
        if not solution_text or not solution_text.strip():
            q_weight = 1.0
            s_weight = 0.0
            solution_text = question_text  # dummy to avoid empty embedding

        vectors = await self.embedding_service.embed_problem_split(question_text, solution_text)
        q_query = np.array(vectors["question_vector"], dtype=np.float32)
        s_query = np.array(vectors["solution_vector"], dtype=np.float32)

        # Filter candidates
        candidates = [
            p for p in self._problems
            if p.get("question_vector") is not None
            and p.get("solution_vector") is not None
            and (exclude_id is None or p.get("id") != exclude_id)
            and (grade is None or str(p.get("grade")) == str(grade))
            and (school_level is None or p.get("school_level") == school_level)
        ]

        if not candidates:
            return []

        q_matrix = np.stack([p["question_vector"] for p in candidates])
        s_matrix = np.stack([p["solution_vector"] for p in candidates])

        q_scores = self._cosine_similarity_vectorized(q_query, q_matrix)
        s_scores = self._cosine_similarity_vectorized(s_query, s_matrix)
        combined = q_weight * q_scores + s_weight * s_scores

        top_indices = np.argpartition(combined, -min(top_k, len(combined)))[-top_k:]
        top_indices = top_indices[np.argsort(combined[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            prob = {k: v for k, v in candidates[idx].items()
                    if k not in ("full_text_vector", "question_vector", "solution_vector")}
            prob["score"] = round(float(combined[idx]), 4)
            prob["question_score"] = round(float(q_scores[idx]), 4)
            prob["solution_score"] = round(float(s_scores[idx]), 4)
            prob["search_type"] = "improved"
            results.append(prob)

        return results

    async def search_compare(
        self, question_text: str, solution_text: str, top_k: int = 10,
        q_weight: float = 0.3, s_weight: float = 0.7,
        grade: int | None = None, school_level: str | None = None,
        exclude_id: int | None = None,
    ) -> dict:
        """Run both legacy and improved search, return side-by-side."""
        full_text = f"{question_text}\n{solution_text}"
        legacy_results, improved_results = await asyncio.gather(
            self.search_legacy(full_text, top_k, grade, school_level, exclude_id),
            self.search_improved(question_text, solution_text, top_k, q_weight, s_weight, grade, school_level, exclude_id),
        )
        return {
            "legacy": legacy_results,
            "improved": improved_results,
        }
