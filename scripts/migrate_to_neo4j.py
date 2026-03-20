"""
migrate_to_neo4j.py
-------------------
Migrate math problem data to Neo4j for graph-based similarity search.

Data sources:
  - SQLite (data/math_problems.db): Problem data + 3072d embeddings
  - MySQL (problem_bank): Tags, tag hierarchy, problem groups, sources, schools

Usage:
    python scripts/migrate_to_neo4j.py [--sqlite-db PATH] [--batch-size N] [--skip-vectors]
"""

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np

# ---------------------------------------------------------------------------
# Allow `from app.config import ...` regardless of CWD
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME  # noqa: E402

import mysql.connector  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402
from tqdm import tqdm  # noqa: E402

# ---------------------------------------------------------------------------
# Neo4j config from .env
# ---------------------------------------------------------------------------
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "changeme")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    # Handle both lists and iterables
    if hasattr(lst, "__len__"):
        for i in range(0, len(lst), n):
            yield lst[i : i + n]
    else:
        batch = []
        for item in lst:
            batch.append(item)
            if len(batch) == n:
                yield batch
                batch = []
        if batch:
            yield batch


def safe_int(val):
    """Convert a value to int safely, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Deactivated tag filtering
# ---------------------------------------------------------------------------

def get_excluded_tag_ids(cursor) -> set:
    """Get all deactivated tag IDs + their descendants recursively."""
    # Step 1: Get directly deactivated tags
    cursor.execute("SELECT id FROM tag WHERE `desc` = 'deactivated'")
    deactivated_ids = {row[0] for row in cursor.fetchall()}

    if not deactivated_ids:
        return set()

    # Step 2: Build parent -> children map
    cursor.execute("SELECT id, parent_id FROM tag WHERE parent_id IS NOT NULL")
    children_map = defaultdict(list)
    for tag_id, parent_id in cursor.fetchall():
        children_map[parent_id].append(tag_id)

    # Step 3: Recursively collect descendants of deactivated tags
    excluded = set(deactivated_ids)

    def collect_descendants(tid):
        for child_id in children_map.get(tid, []):
            if child_id not in excluded:
                excluded.add(child_id)
                collect_descendants(child_id)

    for did in list(deactivated_ids):
        collect_descendants(did)

    return excluded


# ---------------------------------------------------------------------------
# Migration steps
# ---------------------------------------------------------------------------

def step1_load_tags(mysql_cursor):
    """Load active tags from MySQL, filtering out deactivated + descendants."""
    t0 = time.time()
    print("\n[Step 1] Loading tags from MySQL...")

    excluded_ids = get_excluded_tag_ids(mysql_cursor)
    print(f"  Excluded (deactivated + descendants): {len(excluded_ids):,} tags")

    mysql_cursor.execute("SELECT id, parent_id, name, category FROM tag")
    all_tags = mysql_cursor.fetchall()
    print(f"  Total tags in MySQL: {len(all_tags):,}")

    # Filter out excluded tags
    active_tags = [t for t in all_tags if t[0] not in excluded_ids]
    active_tag_ids = {t[0] for t in active_tags}

    # Also filter: only include tags whose parent is active (or parent is NULL)
    active_tags = [t for t in active_tags if t[1] is None or t[1] in active_tag_ids]
    active_tag_ids = {t[0] for t in active_tags}

    print(f"  Active tags: {len(active_tags):,}")
    print(f"  Step 1 done in {time.time() - t0:.1f}s")
    return active_tags, active_tag_ids


def step2_create_tag_nodes(session, active_tags, batch_size):
    """Create Tag nodes in Neo4j."""
    t0 = time.time()
    print("\n[Step 2] Creating Tag nodes...")

    tag_dicts = [
        {"id": t[0], "parent_id": t[1], "name": t[2], "category": t[3]}
        for t in active_tags
    ]

    for batch in tqdm(list(chunks(tag_dicts, batch_size)), desc="  Tags"):
        session.run(
            """
            UNWIND $tags AS t
            MERGE (tag:Tag {id: t.id})
            SET tag.name = t.name, tag.category = t.category, tag.parent_id = t.parent_id
            """,
            tags=batch,
        )

    print(f"  Step 2 done in {time.time() - t0:.1f}s")


def step3_create_tag_hierarchy(session):
    """Create CHILD_OF relationships between active tags."""
    t0 = time.time()
    print("\n[Step 3] Creating CHILD_OF relationships...")

    session.run(
        """
        MATCH (child:Tag) WHERE child.parent_id IS NOT NULL
        MATCH (parent:Tag {id: child.parent_id})
        MERGE (child)-[:CHILD_OF]->(parent)
        """
    )

    print(f"  Step 3 done in {time.time() - t0:.1f}s")


def step4_create_problem_nodes(session, sqlite_db, batch_size, skip_vectors):
    """Load problems from SQLite and create Problem nodes in Neo4j."""
    t0 = time.time()
    print("\n[Step 4] Creating Problem nodes from SQLite...")

    sqlite_conn = sqlite3.connect(sqlite_db)
    sqlite_conn.row_factory = sqlite3.Row
    cursor = sqlite_conn.cursor()

    # Count total
    cursor.execute("SELECT count(*) FROM problems")
    total = cursor.fetchone()[0]
    print(f"  Total problems in SQLite: {total:,}")

    cursor.execute(
        """
        SELECT id, question, answer, grade, school_level, type, level,
               group_id, main_category_tag_id, solution, refer,
               choice1, choice2, choice3, choice4, choice5,
               full_text_vector, question_vector, solution_vector
        FROM problems
        """
    )

    created = 0
    all_rows = cursor.fetchall()
    for batch_rows in tqdm(list(chunks(all_rows, batch_size)), desc="  Problems"):
        problems = []
        for row in batch_rows:
            p = dict(row)

            # Convert grade/level from TEXT to int safely
            p["grade"] = safe_int(p["grade"])
            p["level"] = safe_int(p["level"])
            p["main_category_tag_id"] = safe_int(p["main_category_tag_id"])

            # Convert embedding BLOBs to list[float]
            vec_blob = p.pop("full_text_vector")
            q_vec_blob = p.pop("question_vector")
            s_vec_blob = p.pop("solution_vector")
            if vec_blob and not skip_vectors:
                p["embedding"] = np.frombuffer(vec_blob, dtype=np.float32).tolist()
            else:
                p["embedding"] = None
            if q_vec_blob and not skip_vectors:
                p["question_embedding"] = np.frombuffer(q_vec_blob, dtype=np.float32).tolist()
            else:
                p["question_embedding"] = None
            if s_vec_blob and not skip_vectors:
                p["solution_embedding"] = np.frombuffer(s_vec_blob, dtype=np.float32).tolist()
            else:
                p["solution_embedding"] = None

            # Remove large text fields not needed in Neo4j
            for key in [
                "solution",
                "refer",
                "choice1",
                "choice2",
                "choice3",
                "choice4",
                "choice5",
            ]:
                p.pop(key, None)

            problems.append(p)

        session.run(
            """
            UNWIND $problems AS p
            MERGE (prob:Problem {id: p.id})
            SET prob.question = p.question,
                prob.answer = p.answer,
                prob.grade = p.grade,
                prob.school_level = p.school_level,
                prob.type = p.type,
                prob.level = p.level,
                prob.group_id = p.group_id,
                prob.main_category_tag_id = p.main_category_tag_id,
                prob.embedding = p.embedding,
                prob.question_embedding = p.question_embedding,
                prob.solution_embedding = p.solution_embedding
            """,
            problems=problems,
        )
        created += len(problems)

    sqlite_conn.close()
    print(f"  Created {created:,} Problem nodes")
    print(f"  Step 4 done in {time.time() - t0:.1f}s")
    return total


def step5_create_has_tag(session, mysql_cursor, sqlite_db, active_tag_ids, batch_size):
    """Create HAS_TAG relationships from MySQL problem_tag_bind."""
    t0 = time.time()
    print("\n[Step 5] Creating HAS_TAG relationships...")

    # Get SQLite problem IDs
    sqlite_conn = sqlite3.connect(sqlite_db)
    cursor = sqlite_conn.cursor()
    cursor.execute("SELECT id FROM problems")
    sqlite_problem_ids = {row[0] for row in cursor.fetchall()}
    sqlite_conn.close()
    print(f"  SQLite problem IDs: {len(sqlite_problem_ids):,}")

    # Fetch bindings from MySQL using fetchmany (15.5M rows)
    print("  Reading problem_tag_bind from MySQL (may take a while)...")
    mysql_cursor.execute("SELECT problem_id, tag_id FROM problem_tag_bind")

    bindings = []
    fetched = 0
    while True:
        rows = mysql_cursor.fetchmany(10000)
        if not rows:
            break
        fetched += len(rows)
        for r in rows:
            if r[0] in sqlite_problem_ids and r[1] in active_tag_ids:
                bindings.append({"problem_id": r[0], "tag_id": r[1]})
        if fetched % 1000000 == 0:
            print(f"    Scanned {fetched:,} rows, matched {len(bindings):,}...")

    print(f"  Total scanned: {fetched:,}, matched: {len(bindings):,}")

    # Create HAS_TAG in Neo4j
    for batch in tqdm(list(chunks(bindings, batch_size)), desc="  HAS_TAG"):
        session.run(
            """
            UNWIND $bindings AS b
            MATCH (p:Problem {id: b.problem_id})
            MATCH (t:Tag {id: b.tag_id})
            MERGE (p)-[:HAS_TAG]->(t)
            """,
            bindings=batch,
        )

    print(f"  Step 5 done in {time.time() - t0:.1f}s")


def step6_create_main_tag(session):
    """Create MAIN_TAG relationships from Problem.main_category_tag_id."""
    t0 = time.time()
    print("\n[Step 6] Creating MAIN_TAG relationships...")

    session.run(
        """
        MATCH (p:Problem) WHERE p.main_category_tag_id IS NOT NULL
        MATCH (t:Tag {id: p.main_category_tag_id})
        MERGE (p)-[:MAIN_TAG]->(t)
        """
    )

    print(f"  Step 6 done in {time.time() - t0:.1f}s")


def step7_create_groups_sources_schools(session, mysql_cursor, sqlite_db, batch_size):
    """Create ProblemGroup, Source, School nodes + relationships."""
    t0 = time.time()
    print("\n[Step 7] Creating ProblemGroup, Source, School nodes...")

    # Get unique group_ids from SQLite problems
    sqlite_conn = sqlite3.connect(sqlite_db)
    cursor = sqlite_conn.cursor()
    cursor.execute(
        "SELECT DISTINCT group_id FROM problems WHERE group_id IS NOT NULL"
    )
    group_ids = {row[0] for row in cursor.fetchall()}
    sqlite_conn.close()
    print(f"  Unique group IDs from SQLite: {len(group_ids):,}")

    if not group_ids:
        print("  No groups found, skipping step 7.")
        return

    # ProblemGroup nodes
    mysql_cursor.execute(
        "SELECT id, source_id, instruction FROM problem_group WHERE id IN ({})".format(
            ",".join(str(gid) for gid in group_ids)
        )
    )
    groups = [
        {"id": r[0], "source_id": r[1], "instruction": r[2]}
        for r in mysql_cursor.fetchall()
    ]
    print(f"  ProblemGroups from MySQL: {len(groups):,}")

    for batch in tqdm(list(chunks(groups, batch_size)), desc="  ProblemGroup"):
        session.run(
            """
            UNWIND $groups AS g
            MERGE (pg:ProblemGroup {id: g.id})
            SET pg.instruction = g.instruction, pg.source_id = g.source_id
            """,
            groups=batch,
        )

    # BELONGS_TO_GROUP
    print("  Creating BELONGS_TO_GROUP relationships...")
    session.run(
        """
        MATCH (p:Problem) WHERE p.group_id IS NOT NULL
        MATCH (pg:ProblemGroup {id: p.group_id})
        MERGE (p)-[:BELONGS_TO_GROUP]->(pg)
        """
    )

    # Source nodes
    source_ids = {g["source_id"] for g in groups if g["source_id"]}
    if source_ids:
        mysql_cursor.execute(
            "SELECT id, name, exam_type, year, school_id, math_subject "
            "FROM source_info WHERE id IN ({})".format(
                ",".join(str(sid) for sid in source_ids)
            )
        )
        sources = [
            {
                "id": r[0],
                "name": r[1],
                "exam_type": r[2],
                "year": r[3],
                "school_id": r[4],
                "math_subject": r[5],
            }
            for r in mysql_cursor.fetchall()
        ]
        print(f"  Sources from MySQL: {len(sources):,}")

        for batch in tqdm(list(chunks(sources, batch_size)), desc="  Source"):
            session.run(
                """
                UNWIND $sources AS s
                MERGE (src:Source {id: s.id})
                SET src.name = s.name, src.exam_type = s.exam_type,
                    src.year = s.year, src.school_id = s.school_id,
                    src.math_subject = s.math_subject
                """,
                sources=batch,
            )

        # HAS_SOURCE
        print("  Creating HAS_SOURCE relationships...")
        session.run(
            """
            MATCH (pg:ProblemGroup) WHERE pg.source_id IS NOT NULL
            MATCH (src:Source {id: pg.source_id})
            MERGE (pg)-[:HAS_SOURCE]->(src)
            """
        )

        # School nodes
        school_ids = {s["school_id"] for s in sources if s["school_id"]}
        if school_ids:
            mysql_cursor.execute(
                "SELECT id, kor_name, level, city, district FROM school WHERE id IN ({})".format(
                    ",".join(str(sid) for sid in school_ids)
                )
            )
            schools = [
                {
                    "id": r[0],
                    "kor_name": r[1],
                    "level": r[2],
                    "city": r[3],
                    "district": r[4],
                }
                for r in mysql_cursor.fetchall()
            ]
            print(f"  Schools from MySQL: {len(schools):,}")

            for batch in tqdm(list(chunks(schools, batch_size)), desc="  School"):
                session.run(
                    """
                    UNWIND $schools AS s
                    MERGE (sch:School {id: s.id})
                    SET sch.kor_name = s.kor_name, sch.level = s.level,
                        sch.city = s.city, sch.district = s.district
                    """,
                    schools=batch,
                )

            # FROM_SCHOOL
            print("  Creating FROM_SCHOOL relationships...")
            session.run(
                """
                MATCH (src:Source) WHERE src.school_id IS NOT NULL
                MATCH (sch:School {id: src.school_id})
                MERGE (src)-[:FROM_SCHOOL]->(sch)
                """
            )

    print(f"  Step 7 done in {time.time() - t0:.1f}s")


def step8_create_indexes(session, skip_vectors):
    """Create indexes for performance."""
    t0 = time.time()
    print("\n[Step 8] Creating indexes...")

    # Vector Index
    if not skip_vectors:
        print("  Creating vector index (problem_embedding)...")
        session.run(
            """
            CREATE VECTOR INDEX problem_embedding IF NOT EXISTS
            FOR (p:Problem) ON (p.embedding)
            OPTIONS {indexConfig: {
                `vector.dimensions`: 3072,
                `vector.similarity_function`: 'cosine'
            }}
            """
        )
        print("  Creating vector index (question_embedding)...")
        session.run(
            """
            CREATE VECTOR INDEX question_embedding IF NOT EXISTS
            FOR (p:Problem) ON (p.question_embedding)
            OPTIONS {indexConfig: {
                `vector.dimensions`: 3072,
                `vector.similarity_function`: 'cosine'
            }}
            """
        )
        print("  Creating vector index (solution_embedding)...")
        session.run(
            """
            CREATE VECTOR INDEX solution_embedding IF NOT EXISTS
            FOR (p:Problem) ON (p.solution_embedding)
            OPTIONS {indexConfig: {
                `vector.dimensions`: 3072,
                `vector.similarity_function`: 'cosine'
            }}
            """
        )

    # Property indexes
    index_statements = [
        ("tag_category", "CREATE INDEX tag_category IF NOT EXISTS FOR (t:Tag) ON (t.category)"),
        ("tag_parent", "CREATE INDEX tag_parent IF NOT EXISTS FOR (t:Tag) ON (t.parent_id)"),
        ("problem_grade", "CREATE INDEX problem_grade IF NOT EXISTS FOR (p:Problem) ON (p.grade)"),
        ("problem_school_level", "CREATE INDEX problem_school_level IF NOT EXISTS FOR (p:Problem) ON (p.school_level)"),
        ("problem_group_id", "CREATE INDEX problem_group_id IF NOT EXISTS FOR (p:Problem) ON (p.group_id)"),
    ]

    for name, stmt in index_statements:
        print(f"  Creating index: {name}")
        session.run(stmt)

    print(f"  Step 8 done in {time.time() - t0:.1f}s")


def step9_verify(session):
    """Run verification queries."""
    print("\n=== Verification ===")

    results = {}
    queries = [
        ("Problems", "MATCH (p:Problem) RETURN count(p) AS cnt"),
        ("Tags", "MATCH (t:Tag) RETURN count(t) AS cnt"),
        ("HAS_TAG", "MATCH ()-[r:HAS_TAG]->() RETURN count(r) AS cnt"),
        ("CHILD_OF", "MATCH ()-[r:CHILD_OF]->() RETURN count(r) AS cnt"),
        ("MAIN_TAG", "MATCH ()-[r:MAIN_TAG]->() RETURN count(r) AS cnt"),
        ("BELONGS_TO_GROUP", "MATCH ()-[r:BELONGS_TO_GROUP]->() RETURN count(r) AS cnt"),
        ("ProblemGroups", "MATCH (pg:ProblemGroup) RETURN count(pg) AS cnt"),
        ("Sources", "MATCH (s:Source) RETURN count(s) AS cnt"),
        ("Schools", "MATCH (s:School) RETURN count(s) AS cnt"),
    ]

    for label, query in queries:
        result = session.run(query).single()
        cnt = result["cnt"]
        results[label] = cnt
        print(f"  {label}: {cnt:,}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate math problem data to Neo4j graph database."
    )
    parser.add_argument(
        "--sqlite-db",
        default="data/math_problems.db",
        help="Path to SQLite database (default: data/math_problems.db)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for Neo4j UNWIND operations (default: 500)",
    )
    parser.add_argument(
        "--skip-vectors",
        action="store_true",
        help="Skip embedding vectors (faster for testing)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all Neo4j data before migration",
    )
    parser.add_argument(
        "--vectors-only",
        action="store_true",
        help="Only update vectors on existing Problem nodes + rebuild vector indexes (skip tags/relationships)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    overall_t0 = time.time()

    # Resolve paths
    project_root = os.path.join(os.path.dirname(__file__), "..")
    sqlite_db = (
        args.sqlite_db
        if os.path.isabs(args.sqlite_db)
        else os.path.join(project_root, args.sqlite_db)
    )

    print("=== Neo4j Migration ===")
    print(f"SQLite: {sqlite_db}")
    print(f"Neo4j:  {NEO4J_URI}")
    print(f"Batch size: {args.batch_size}")
    if args.skip_vectors:
        print("Vectors: SKIPPED")
    if args.vectors_only:
        print("Mode: VECTORS-ONLY (Step 4 + Step 8 only)")
    print()

    # --- Connect ---
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    print("Neo4j connected.")

    if not os.path.exists(sqlite_db):
        print(f"ERROR: SQLite database not found: {sqlite_db}")
        sys.exit(1)
    print("SQLite verified.")

    if args.vectors_only:
        # Fast path: only update vectors and indexes (no MySQL needed)
        with driver.session() as session:
            step4_create_problem_nodes(session, sqlite_db, args.batch_size, args.skip_vectors)
            step8_create_indexes(session, args.skip_vectors)
            step9_verify(session)
        driver.close()
    else:
        print(f"MySQL:  {DB_HOST}:{DB_PORT}/{DB_NAME}")
        mysql_conn = mysql.connector.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
        )
        mysql_cursor = mysql_conn.cursor()
        print("MySQL connected.")

        # --- Migrate ---
        with driver.session() as session:
            if args.clear:
                print("\nClearing all Neo4j data...")
                session.run("MATCH (n) DETACH DELETE n")
                print("Cleared.")

            # Step 1: Load active tags
            active_tags, active_tag_ids = step1_load_tags(mysql_cursor)

            # Step 2: Create Tag nodes
            step2_create_tag_nodes(session, active_tags, args.batch_size)

            # Step 3: CHILD_OF relationships
            step3_create_tag_hierarchy(session)

            # Step 4: Problem nodes from SQLite
            step4_create_problem_nodes(session, sqlite_db, args.batch_size, args.skip_vectors)

            # Step 5: HAS_TAG from MySQL problem_tag_bind
            step5_create_has_tag(session, mysql_cursor, sqlite_db, active_tag_ids, args.batch_size)

            # Step 6: MAIN_TAG relationships
            step6_create_main_tag(session)

            # Step 7: ProblemGroup, Source, School
            step7_create_groups_sources_schools(session, mysql_cursor, sqlite_db, args.batch_size)

            # Step 8: Indexes
            step8_create_indexes(session, args.skip_vectors)

            # Step 9: Verification
            step9_verify(session)

        # --- Cleanup ---
        driver.close()
        mysql_conn.close()

    elapsed = time.time() - overall_t0
    print(f"\nMigration complete! Total time: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
