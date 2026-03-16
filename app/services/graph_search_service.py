"""Graph-based similarity search using Neo4j vector index + tag structure."""

import sqlite3
from collections import Counter
from pathlib import Path

from app.db.neo4j_client import Neo4jClient
from app.services.embedding_service import EmbeddingService


class GraphSearchService:
    """Neo4j hybrid search: vector similarity + tag structure similarity."""

    def __init__(self, neo4j_client: Neo4jClient):
        self.neo4j = neo4j_client
        self.embedding_service = EmbeddingService()
        self._sqlite_db_path: str | None = None

    def set_sqlite_path(self, path: str):
        self._sqlite_db_path = path

    def _enrich_from_sqlite(self, results: list[dict]) -> list[dict]:
        """Fetch full problem details from SQLite for result IDs."""
        if not self._sqlite_db_path or not results:
            return results

        ids = [r["id"] for r in results]
        placeholders = ",".join("?" * len(ids))

        conn = sqlite3.connect(self._sqlite_db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT id, question, refer, answer, solution, "
                f"choice1, choice2, choice3, choice4, choice5, "
                f"grade, school_level, type, level, tag_ids, main_category_tag_id, "
                f"instruction, source_name, exam_type, year, school_name, city "
                f"FROM problems WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            sqlite_map = {row["id"]: dict(row) for row in rows}

            for r in results:
                detail = sqlite_map.get(r["id"], {})
                # Merge SQLite fields into result (don't overwrite score fields)
                for key, val in detail.items():
                    if key not in r:
                        r[key] = val
        finally:
            conn.close()

        return results

    async def search_hybrid(
        self,
        problem_id: int | None,
        question: str,
        solution: str,
        top_k: int = 10,
        alpha: float = 0.6,
        beta: float = 0.4,
        grade: int | None = None,
        school_level: str | None = None,
        exclude_id: int | None = None,
    ) -> list[dict]:
        """Hybrid search combining vector similarity + tag structure.

        Mode A: problem_id given → use graph node's embedding + tags
        Mode B: free text → embed text, use vector search + inferred tags
        """
        if problem_id is not None:
            results = self._search_mode_a(
                problem_id, top_k, alpha, beta, grade, school_level
            )
        else:
            # Generate embedding from text
            full_text = f"{question}\n{solution}" if solution else question
            query_embedding = await self.embedding_service.embed_text(full_text)
            results = self._search_mode_b(
                query_embedding, top_k, alpha, beta, grade, school_level, exclude_id
            )

        # Enrich with SQLite data
        results = self._enrich_from_sqlite(results)

        # Add search_type
        for r in results:
            r["search_type"] = "graph"

        return results

    def _search_mode_a(
        self,
        query_id: int,
        top_k: int,
        alpha: float,
        beta: float,
        grade: int | None,
        school_level: str | None,
    ) -> list[dict]:
        """Mode A: Problem-ID based hybrid search using single batch Cypher."""
        candidate_k = top_k * 3  # fetch more candidates for tag scoring

        query = """
        MATCH (query:Problem {id: $query_id})
        WITH query, query.embedding AS qvec

        OPTIONAL MATCH (query)-[:HAS_TAG]->(qt:Tag)
        WITH query, qvec,
             collect({id: qt.id, category: qt.category}) AS query_tags

        CALL db.index.vector.queryNodes('problem_embedding', $candidate_k, qvec)
        YIELD node AS candidate, score AS vec_score
        WHERE candidate.id <> $query_id
          AND ($grade IS NULL OR candidate.grade = $grade)
          AND ($school_level IS NULL OR candidate.school_level = $school_level)

        WITH candidate, vec_score, query_tags
        OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)
        WITH candidate, vec_score, query_tags,
             collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

        WITH candidate, vec_score, query_tags, cand_tags,
             CASE
               WHEN size(query_tags) = 0 THEN 0.0
               WHEN size([qt IN query_tags WHERE qt.id IN [ct IN cand_tags | ct.id]]) = 0 THEN 0.0
               ELSE toFloat(
                 reduce(s = 0.0, st IN [qt IN query_tags WHERE qt.id IN [ct IN cand_tags | ct.id]] |
                   s + CASE st.category
                     WHEN 'depth5' THEN 5.0
                     WHEN 'depth4' THEN 4.0
                     WHEN 'depth3' THEN 3.0
                     WHEN 'depth2' THEN 2.0
                     WHEN 'depth1' THEN 1.0
                     ELSE 0.5
                   END
                 )
               ) / toFloat(
                 reduce(s = 0.0, qt IN query_tags |
                   s + CASE qt.category
                     WHEN 'depth5' THEN 5.0
                     WHEN 'depth4' THEN 4.0
                     WHEN 'depth3' THEN 3.0
                     WHEN 'depth2' THEN 2.0
                     WHEN 'depth1' THEN 1.0
                     ELSE 0.5
                   END
                 )
               )
             END AS tag_score

        WITH candidate, vec_score, tag_score, cand_tags, query_tags,
             ($alpha * vec_score + $beta * tag_score) AS final_score,
             [ct IN cand_tags WHERE ct.id IN [qt IN query_tags | qt.id]] AS shared_tags_info
        ORDER BY final_score DESC
        LIMIT $top_k

        RETURN candidate.id AS id,
               round(final_score, 4) AS score,
               round(vec_score, 4) AS vector_score,
               round(tag_score, 4) AS graph_score,
               [st IN shared_tags_info | st.name] AS shared_tags,
               false AS graph_score_inferred
        """

        records = self.neo4j.execute_query(
            query,
            query_id=query_id,
            candidate_k=candidate_k,
            top_k=top_k,
            alpha=alpha,
            beta=beta,
            grade=grade,
            school_level=school_level,
        )
        return records

    def _search_mode_b(
        self,
        query_embedding: list[float],
        top_k: int,
        alpha: float,
        beta: float,
        grade: int | None,
        school_level: str | None,
        exclude_id: int | None,
    ) -> list[dict]:
        """Mode B: Free-text vector search + inferred tag scoring."""
        candidate_k = top_k * 3

        query = """
        CALL db.index.vector.queryNodes('problem_embedding', $candidate_k, $query_embedding)
        YIELD node AS candidate, score AS vec_score
        WHERE ($exclude_id IS NULL OR candidate.id <> $exclude_id)
          AND ($grade IS NULL OR candidate.grade = $grade)
          AND ($school_level IS NULL OR candidate.school_level = $school_level)

        WITH candidate, vec_score
        OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)
        WITH candidate, vec_score,
             collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

        ORDER BY vec_score DESC
        LIMIT $candidate_k

        RETURN candidate.id AS id,
               round(vec_score, 4) AS vector_score,
               cand_tags AS tags
        """

        records = self.neo4j.execute_query(
            query,
            query_embedding=query_embedding,
            candidate_k=candidate_k,
            exclude_id=exclude_id,
            grade=grade,
            school_level=school_level,
        )

        # Python post-processing: infer tags from top candidates
        if not records:
            return []

        # Step 1: Find most common depth3~5 tags in top 10 candidates
        top_candidates = records[:10]
        tag_freq: Counter = Counter()
        for c in top_candidates:
            for t in c.get("tags", []):
                if t["category"] in ("depth3", "depth4", "depth5"):
                    tag_freq[t["id"]] += 1
        inferred_tags = {tid for tid, cnt in tag_freq.most_common(5) if cnt >= 2}

        # Step 2: Compute graph_score for each candidate
        results = []
        for c in records:
            cand_tag_ids = {t["id"] for t in c.get("tags", [])}
            shared_count = len(cand_tag_ids & inferred_tags)
            graph_score = shared_count / max(len(inferred_tags), 1)

            shared_tag_names = [
                t["name"] for t in c.get("tags", []) if t["id"] in inferred_tags
            ]

            final_score = alpha * c["vector_score"] + beta * graph_score

            results.append({
                "id": c["id"],
                "score": round(final_score, 4),
                "vector_score": c["vector_score"],
                "graph_score": round(graph_score, 4),
                "shared_tags": shared_tag_names,
                "graph_score_inferred": True,
            })

        # Sort by final score and limit
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def search_by_graph_traversal(
        self,
        problem_id: int,
        top_k: int = 10,
        grade: int | None = None,
    ) -> list[dict]:
        """Pure graph traversal: find problems sharing depth3~5 tags."""
        query = """
        MATCH (query:Problem {id: $problem_id})-[:HAS_TAG]->(qt:Tag)
        WHERE qt.category IN ['depth3', 'depth4', 'depth5']

        MATCH (other:Problem)-[:HAS_TAG]->(qt)
        WHERE other.id <> $problem_id
          AND ($grade IS NULL OR other.grade = $grade)

        WITH other,
             collect(DISTINCT qt.name) AS shared_tags,
             count(DISTINCT qt) AS shared_count,
             sum(CASE qt.category
               WHEN 'depth5' THEN 5 WHEN 'depth4' THEN 4
               WHEN 'depth3' THEN 3 ELSE 1 END) AS weighted_count
        ORDER BY weighted_count DESC
        LIMIT $top_k

        RETURN other.id AS id,
               shared_tags,
               shared_count,
               weighted_count AS score
        """

        records = self.neo4j.execute_query(
            query,
            problem_id=problem_id,
            top_k=top_k,
            grade=grade,
        )

        results = self._enrich_from_sqlite(records)
        for r in results:
            r["search_type"] = "graph"
            r["graph_score_inferred"] = False
        return results
