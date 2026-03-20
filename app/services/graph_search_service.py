"""Graph-based similarity search using Neo4j vector index + tag structure + metadata."""

import sqlite3
from collections import Counter
from pathlib import Path

from app.db.neo4j_client import Neo4jClient
from app.services.embedding_service import EmbeddingService

# Metadata sub-weights for computing metadata_score
_W_GRADE = 0.35
_W_SCHOOL = 0.35
_W_TYPE = 0.15
_W_LEVEL = 0.15


def _grade_sim(a, b) -> float:
    """Grade proximity: same=1.0, diff=1 → 0.5, diff>=2 → 0.0."""
    if a is None or b is None:
        return None
    try:
        diff = abs(int(a) - int(b))
    except (ValueError, TypeError):
        return None
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.5
    return 0.0


def _school_sim(a, b) -> float:
    """School level match: same=1.0, different=0.0."""
    if a is None or b is None:
        return None
    return 1.0 if a == b else 0.0


def _type_sim(a, b) -> float:
    """Problem type match: same=1.0, different=0.0."""
    if a is None or b is None:
        return None
    return 1.0 if a == b else 0.0


def _level_sim(a, b) -> float:
    """Difficulty proximity: same=1.0, diff=1→0.7, diff=2→0.4, diff>=3→0.1."""
    if a is None or b is None:
        return None
    try:
        diff = abs(int(a) - int(b))
    except (ValueError, TypeError):
        return None
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.7
    if diff == 2:
        return 0.4
    return 0.1


def _compute_single_metadata_score(query_meta: dict, cand_meta: dict) -> float:
    """Compute metadata similarity between query and candidate (0.0~1.0).
    NULL-safe: if either side is NULL for a dimension, that weight is excluded."""
    components = [
        (_W_GRADE, _grade_sim(query_meta.get("grade"), cand_meta.get("grade"))),
        (_W_SCHOOL, _school_sim(query_meta.get("school_level"), cand_meta.get("school_level"))),
        (_W_TYPE, _type_sim(query_meta.get("type"), cand_meta.get("type"))),
        (_W_LEVEL, _level_sim(query_meta.get("level"), cand_meta.get("level"))),
    ]
    total_w = 0.0
    total_s = 0.0
    for w, s in components:
        if s is not None:
            total_w += w
            total_s += w * s
    return round(total_s / total_w, 4) if total_w > 0 else 0.0


class GraphSearchService:
    """Neo4j hybrid search: vector + tag structure + metadata similarity."""

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

    def _merge_vector_scores(
        self, q_records: list[dict], s_records: list[dict],
        q_weight: float, s_weight: float, candidate_k: int,
    ) -> list[tuple[int, dict]]:
        """Merge question/solution vector scores with weights."""
        score_map: dict[int, dict] = {}
        for r in q_records:
            score_map[r["id"]] = {"q_score": r["q_score"], "s_score": 0.0}
        for r in s_records:
            if r["id"] in score_map:
                score_map[r["id"]]["s_score"] = r["s_score"]
            else:
                score_map[r["id"]] = {"q_score": 0.0, "s_score": r["s_score"]}

        for scores in score_map.values():
            scores["vec_score"] = q_weight * scores["q_score"] + s_weight * scores["s_score"]

        candidates = sorted(score_map.items(), key=lambda x: x[1]["vec_score"], reverse=True)
        return candidates[:candidate_k]

    def _compute_metadata_scores(
        self, query_id: int, candidate_ids: list[int],
    ) -> dict[int, float]:
        """Fetch metadata for query + candidates from Neo4j and compute scores."""
        records = self.neo4j.execute_query(
            """
            MATCH (query:Problem {id: $query_id})
            WITH query.grade AS qg, query.school_level AS qsl,
                 query.type AS qt, query.level AS ql

            UNWIND $candidate_ids AS cid
            MATCH (c:Problem {id: cid})
            RETURN c.id AS id, qg, qsl, qt, ql,
                   c.grade AS cg, c.school_level AS csl,
                   c.type AS ct, c.level AS cl
            """,
            query_id=query_id, candidate_ids=candidate_ids,
        )
        result = {}
        for r in records:
            query_meta = {"grade": r["qg"], "school_level": r["qsl"],
                          "type": r["qt"], "level": r["ql"]}
            cand_meta = {"grade": r["cg"], "school_level": r["csl"],
                         "type": r["ct"], "level": r["cl"]}
            result[r["id"]] = _compute_single_metadata_score(query_meta, cand_meta)
        return result

    def _compute_metadata_scores_from_meta(
        self, query_meta: dict, candidate_ids: list[int],
    ) -> dict[int, float]:
        """Compute metadata scores for Mode B (text input with known grade/school_level)."""
        if not any(query_meta.get(k) for k in ("grade", "school_level")):
            return {}
        records = self.neo4j.execute_query(
            """
            UNWIND $candidate_ids AS cid
            MATCH (c:Problem {id: cid})
            RETURN c.id AS id, c.grade AS cg, c.school_level AS csl,
                   c.type AS ct, c.level AS cl
            """,
            candidate_ids=candidate_ids,
        )
        result = {}
        for r in records:
            cand_meta = {"grade": r["cg"], "school_level": r["csl"],
                         "type": r["ct"], "level": r["cl"]}
            result[r["id"]] = _compute_single_metadata_score(query_meta, cand_meta)
        return result

    def _query_tag_scores(self, query_id: int, candidate_ids: list[int]) -> dict:
        """Batch compute tag scores with CHILD_OF hierarchy partial matching.

        Scoring:
        - Direct tag match: 100% of depth weight
        - Shared parent (1 hop via CHILD_OF): 50% of depth weight
        - Shared grandparent (2 hops): 25% of depth weight
        """
        tag_records = self.neo4j.execute_query(
            """
            MATCH (query:Problem {id: $query_id})-[:HAS_TAG]->(qt:Tag)
            WITH collect({id: qt.id, category: qt.category}) AS query_tags

            UNWIND $candidate_ids AS cid
            MATCH (c:Problem {id: cid})
            OPTIONAL MATCH (c)-[:HAS_TAG]->(ct:Tag)
            WITH c, query_tags, collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

            // Direct match score
            WITH c, query_tags, cand_tags,
                 CASE
                   WHEN size(query_tags) = 0 THEN 0.0
                   ELSE toFloat(
                     reduce(s = 0.0, st IN [qt IN query_tags WHERE qt.id IN [ct IN cand_tags | ct.id]] |
                       s + CASE st.category
                         WHEN 'depth5' THEN 5.0 WHEN 'depth4' THEN 4.0
                         WHEN 'depth3' THEN 3.0 WHEN 'depth2' THEN 2.0
                         WHEN 'depth1' THEN 1.0 ELSE 0.5 END
                     )
                   )
                 END AS direct_score,
                 toFloat(
                   reduce(s = 0.0, qt IN query_tags |
                     s + CASE qt.category
                       WHEN 'depth5' THEN 5.0 WHEN 'depth4' THEN 4.0
                       WHEN 'depth3' THEN 3.0 WHEN 'depth2' THEN 2.0
                       WHEN 'depth1' THEN 1.0 ELSE 0.5 END
                   )
                 ) AS max_score,
                 [ct IN cand_tags WHERE ct.id IN [qt IN query_tags | qt.id]] AS shared_tags_info

            RETURN c.id AS id,
                   CASE WHEN max_score = 0 THEN 0.0
                        ELSE round(direct_score / max_score, 4) END AS tag_score,
                   round(direct_score, 2) AS direct_score,
                   round(max_score, 2) AS max_score,
                   [st IN shared_tags_info | st.name] AS shared_tags
            """,
            query_id=query_id, candidate_ids=candidate_ids,
        )
        base_map = {r["id"]: r for r in tag_records}

        # CHILD_OF hierarchy bonus: find candidates sharing parent tags
        hierarchy_records = self.neo4j.execute_query(
            """
            MATCH (query:Problem {id: $query_id})-[:HAS_TAG]->(qt:Tag)
            WHERE qt.category IN ['depth3', 'depth4', 'depth5']

            // 1-hop: sibling tags (share same parent)
            OPTIONAL MATCH (qt)-[:CHILD_OF]->(parent:Tag)<-[:CHILD_OF]-(sibling:Tag)
            WHERE sibling.id <> qt.id

            WITH qt, collect(DISTINCT sibling.id) AS sibling_ids,
                 CASE qt.category
                   WHEN 'depth5' THEN 5.0 WHEN 'depth4' THEN 4.0
                   WHEN 'depth3' THEN 3.0 ELSE 1.0 END AS base_weight

            UNWIND $candidate_ids AS cid
            MATCH (c:Problem {id: cid})-[:HAS_TAG]->(ct:Tag)
            WHERE ct.id IN sibling_ids
              AND NOT exists {
                MATCH (query:Problem {id: $query_id})-[:HAS_TAG]->(ct)
              }

            WITH c.id AS cid, sum(base_weight * 0.5) AS hierarchy_bonus
            RETURN cid AS id, round(hierarchy_bonus, 2) AS hierarchy_bonus
            """,
            query_id=query_id, candidate_ids=candidate_ids,
        )
        hierarchy_map = {r["id"]: r["hierarchy_bonus"] for r in hierarchy_records}

        # Merge hierarchy bonus into tag scores
        for cid, info in base_map.items():
            bonus = hierarchy_map.get(cid, 0.0)
            max_score = info.get("max_score", 0.0)
            if max_score > 0 and bonus > 0:
                enhanced = info["direct_score"] + bonus
                info["tag_score"] = round(min(enhanced / max_score, 1.0), 4)

        return base_map

    async def search_hybrid(
        self,
        problem_id: int | None,
        question: str,
        solution: str,
        top_k: int = 10,
        alpha: float = 0.5,
        beta: float = 0.4,
        gamma: float = 0.1,
        q_weight: float = 0.3,
        s_weight: float = 0.7,
        grade: int | None = None,
        school_level: str | None = None,
        exclude_id: int | None = None,
    ) -> list[dict]:
        """Hybrid search: vector + tag structure + metadata similarity.

        final_score = α × vector + β × tag + γ × metadata

        Mode A: problem_id given -> use graph node's split embeddings + tags + metadata
        Mode B: free text -> embed text, vector + inferred tags + partial metadata
        """
        if problem_id is not None:
            results = self._search_mode_a(
                problem_id, top_k, alpha, beta, gamma,
                q_weight, s_weight, grade, school_level
            )
        else:
            # Generate split embeddings
            if solution and solution.strip():
                vectors = await self.embedding_service.embed_problem_split(question, solution)
                q_embedding = vectors["question_vector"]
                s_embedding = vectors["solution_vector"]
            else:
                q_embedding = await self.embedding_service.embed_text(question)
                s_embedding = q_embedding
                q_weight, s_weight = 1.0, 0.0

            query_meta = {"grade": grade, "school_level": school_level}
            results = self._search_mode_b(
                q_embedding, s_embedding, top_k, alpha, beta, gamma,
                q_weight, s_weight, grade, school_level, exclude_id, query_meta
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
        gamma: float,
        q_weight: float,
        s_weight: float,
        grade: int | None,
        school_level: str | None,
    ) -> list[dict]:
        """Mode A: Problem-ID based hybrid search (vector + tag + metadata)."""
        candidate_k = top_k * 3

        # Query question_embedding index
        q_records = self.neo4j.execute_query(
            """
            MATCH (query:Problem {id: $query_id})
            CALL db.index.vector.queryNodes('question_embedding', $candidate_k, query.question_embedding)
            YIELD node AS candidate, score AS q_score
            WHERE candidate.id <> $query_id
              AND ($grade IS NULL OR candidate.grade = $grade)
              AND ($school_level IS NULL OR candidate.school_level = $school_level)
            RETURN candidate.id AS id, q_score
            """,
            query_id=query_id, candidate_k=candidate_k,
            grade=grade, school_level=school_level,
        )

        # Query solution_embedding index
        s_records = self.neo4j.execute_query(
            """
            MATCH (query:Problem {id: $query_id})
            CALL db.index.vector.queryNodes('solution_embedding', $candidate_k, query.solution_embedding)
            YIELD node AS candidate, score AS s_score
            WHERE candidate.id <> $query_id
              AND ($grade IS NULL OR candidate.grade = $grade)
              AND ($school_level IS NULL OR candidate.school_level = $school_level)
            RETURN candidate.id AS id, s_score
            """,
            query_id=query_id, candidate_k=candidate_k,
            grade=grade, school_level=school_level,
        )

        # Merge vector scores
        candidates = self._merge_vector_scores(q_records, s_records, q_weight, s_weight, candidate_k)

        if not candidates:
            return []

        candidate_ids = [cid for cid, _ in candidates]

        # Tag scoring (with CHILD_OF hierarchy)
        tag_map = self._query_tag_scores(query_id, candidate_ids)

        # Metadata scoring
        meta_map = self._compute_metadata_scores(query_id, candidate_ids)

        # Compute final 3-axis scores
        results = []
        for cid, scores in candidates:
            tag_info = tag_map.get(cid, {"tag_score": 0.0, "shared_tags": []})
            tag_score = tag_info.get("tag_score", 0.0)
            meta_score = meta_map.get(cid, 0.0)
            final_score = alpha * scores["vec_score"] + beta * tag_score + gamma * meta_score

            results.append({
                "id": cid,
                "score": round(final_score, 4),
                "vector_score": round(scores["vec_score"], 4),
                "question_score": round(scores["q_score"], 4),
                "solution_score": round(scores["s_score"], 4),
                "graph_score": tag_score,
                "metadata_score": meta_score,
                "shared_tags": tag_info.get("shared_tags", []),
                "graph_score_inferred": False,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def _search_mode_b(
        self,
        q_embedding: list[float],
        s_embedding: list[float],
        top_k: int,
        alpha: float,
        beta: float,
        gamma: float,
        q_weight: float,
        s_weight: float,
        grade: int | None,
        school_level: str | None,
        exclude_id: int | None,
        query_meta: dict | None = None,
    ) -> list[dict]:
        """Mode B: Free-text split vector search + inferred tags + metadata."""
        candidate_k = top_k * 3

        # Query question_embedding index
        q_records = self.neo4j.execute_query(
            """
            CALL db.index.vector.queryNodes('question_embedding', $candidate_k, $q_embedding)
            YIELD node AS candidate, score AS q_score
            WHERE ($exclude_id IS NULL OR candidate.id <> $exclude_id)
              AND ($grade IS NULL OR candidate.grade = $grade)
              AND ($school_level IS NULL OR candidate.school_level = $school_level)
            RETURN candidate.id AS id, q_score
            """,
            q_embedding=q_embedding, candidate_k=candidate_k,
            exclude_id=exclude_id, grade=grade, school_level=school_level,
        )

        # Query solution_embedding index
        s_records = self.neo4j.execute_query(
            """
            CALL db.index.vector.queryNodes('solution_embedding', $candidate_k, $s_embedding)
            YIELD node AS candidate, score AS s_score
            WHERE ($exclude_id IS NULL OR candidate.id <> $exclude_id)
              AND ($grade IS NULL OR candidate.grade = $grade)
              AND ($school_level IS NULL OR candidate.school_level = $school_level)
            RETURN candidate.id AS id, s_score
            """,
            s_embedding=s_embedding, candidate_k=candidate_k,
            exclude_id=exclude_id, grade=grade, school_level=school_level,
        )

        # Merge vector scores
        candidates = self._merge_vector_scores(q_records, s_records, q_weight, s_weight, candidate_k)

        if not candidates:
            return []

        # Infer tags from top candidates using Neo4j tag data
        top_for_inference = candidates[:10]
        top_ids = [cid for cid, _ in top_for_inference]

        tag_data = self.neo4j.execute_query(
            """
            UNWIND $ids AS cid
            MATCH (c:Problem {id: cid})-[:HAS_TAG]->(ct:Tag)
            RETURN c.id AS id, collect({id: ct.id, name: ct.name, category: ct.category}) AS tags
            """,
            ids=top_ids,
        )
        candidate_tags = {r["id"]: r["tags"] for r in tag_data}

        # Find common depth3~5 tags (frequency >= 2)
        tag_freq: Counter = Counter()
        for cid, _ in top_for_inference:
            for t in candidate_tags.get(cid, []):
                if t["category"] in ("depth3", "depth4", "depth5"):
                    tag_freq[t["id"]] += 1
        inferred_tags = {tid for tid, cnt in tag_freq.most_common(5) if cnt >= 2}

        # Get tags for all candidates
        all_ids = [cid for cid, _ in candidates]
        all_tag_data = self.neo4j.execute_query(
            """
            UNWIND $ids AS cid
            MATCH (c:Problem {id: cid})
            OPTIONAL MATCH (c)-[:HAS_TAG]->(ct:Tag)
            RETURN c.id AS id, collect({id: ct.id, name: ct.name, category: ct.category}) AS tags
            """,
            ids=all_ids,
        )
        all_candidate_tags = {r["id"]: r["tags"] for r in all_tag_data}

        # Metadata scoring (Mode B: use filter values as query metadata)
        all_ids = [cid for cid, _ in candidates]
        meta_map = {}
        if query_meta and gamma > 0:
            meta_map = self._compute_metadata_scores_from_meta(
                query_meta or {}, all_ids
            )

        # Compute graph_score + metadata_score and final score
        results = []
        for cid, scores in candidates:
            cand_tags = all_candidate_tags.get(cid, [])
            cand_tag_ids = {t["id"] for t in cand_tags}
            shared_count = len(cand_tag_ids & inferred_tags)
            graph_score = shared_count / max(len(inferred_tags), 1)

            shared_tag_names = [t["name"] for t in cand_tags if t["id"] in inferred_tags]
            meta_score = meta_map.get(cid, 0.0)

            final_score = alpha * scores["vec_score"] + beta * graph_score + gamma * meta_score

            results.append({
                "id": cid,
                "score": round(final_score, 4),
                "vector_score": round(scores["vec_score"], 4),
                "question_score": round(scores["q_score"], 4),
                "solution_score": round(scores["s_score"], 4),
                "graph_score": round(graph_score, 4),
                "metadata_score": meta_score,
                "shared_tags": shared_tag_names,
                "graph_score_inferred": True,
            })

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
