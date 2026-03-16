"""
extract_problems.py
-------------------
Extract ~10K random math problems from the production MySQL database and save
them to a SQLite database for use in similarity analysis.

Usage:
    python scripts/extract_problems.py [--limit N] [--output PATH]

Output:
    data/math_problems.db   – SQLite database with problems table

Environment variables (loaded from .env in project root):
    OCI_DB_PROD_HOST      (default: localhost)
    OCI_DB_PROD_PORT      (default: 33108)
    OCI_DB_PROD_USER
    OCI_DB_PROD_PASSWORD
    OCI_DB_PROD_NAME      (default: problem_bank)
"""

import argparse
import html
import os
import re
import sqlite3
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Allow `from app.config import ...` regardless of CWD
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME  # noqa: E402

import mysql.connector
from mysql.connector import pooling
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------------------------
TABLE_ATTRS = {"style", "width", "height", "cellpadding", "cellspacing", "border"}


def clean_html(raw: str) -> str:
    """Remove img tags, strip noisy table attrs, unescape HTML entities."""
    soup = BeautifulSoup(raw or "", "html.parser")
    for img in soup.find_all("img"):
        img.decompose()
    for tag in soup.find_all(["table", "tr", "td", "th"]):
        for attr in list(tag.attrs):
            if attr.lower() in TABLE_ATTRS:
                del tag.attrs[attr]
    txt = html.unescape(str(soup))
    return re.sub(r"\s+", " ", txt).strip()


# ---------------------------------------------------------------------------
# Text field builders
# ---------------------------------------------------------------------------

def _choices_text(row: dict) -> str:
    parts = []
    for i in range(1, 6):
        val = row.get(f"choice{i}") or ""
        cleaned = clean_html(val)
        if cleaned:
            parts.append(f"{i}. {cleaned}")
    return "  ".join(parts)


def build_full_text(row: dict) -> str:
    question = clean_html(row.get("question") or "")
    refer = clean_html(row.get("refer") or "")
    choices = _choices_text(row)
    solution = clean_html(row.get("solution") or "")

    parts = []
    if question:
        parts.append(f"발문: {question}")
    if refer:
        parts.append(f"보기: {refer}")
    if choices:
        parts.append(f"선지: {choices}")
    if solution:
        parts.append(f"해설: {solution}")
    return "\n".join(parts)


def build_question_text(row: dict) -> str:
    question = clean_html(row.get("question") or "")
    refer = clean_html(row.get("refer") or "")
    choices = _choices_text(row)

    parts = []
    if question:
        parts.append(f"발문: {question}")
    if refer:
        parts.append(f"보기: {refer}")
    if choices:
        parts.append(f"선지: {choices}")
    return "\n".join(parts)


def build_solution_text(row: dict) -> str:
    return clean_html(row.get("solution") or "")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
SQL_QUERY = """
SELECT p.id, p.group_id, p.question, p.refer, p.answer, p.solution,
       p.choice1, p.choice2, p.choice3, p.choice4, p.choice5,
       p.grade, p.school_level, p.type, p.level,
       p.tag_ids, p.main_category_tag_id,
       pg.instruction, si.name as source_name, si.exam_type, si.year,
       sc.kor_name as school_name, sc.city
FROM problem p
LEFT JOIN problem_group pg ON pg.id = p.group_id
LEFT JOIN source_info si ON si.id = pg.source_id
LEFT JOIN school sc ON sc.id = si.school_id
WHERE p.subject = 'math' AND p.is_serving = 1
ORDER BY RAND()
LIMIT %s;
"""


def create_pool() -> pooling.MySQLConnectionPool:
    pool_cfg = {
        "pool_name": "extract_pool",
        "pool_size": 3,
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "charset": "utf8mb4",
        "use_unicode": True,
    }
    return pooling.MySQLConnectionPool(**pool_cfg)


def fetch_problems(limit: int) -> list[dict]:
    pool = create_pool()
    conn = pool.get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(SQL_QUERY, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def row_to_record(row: dict) -> dict:
    """Enrich a raw DB row with derived text fields."""
    record = dict(row)
    # Ensure byte fields are converted to str (tag_ids might be bytes in some drivers)
    for key, val in record.items():
        if isinstance(val, (bytes, bytearray)):
            record[key] = val.decode("utf-8", errors="replace")

    record["full_text"] = build_full_text(record)
    record["question_text"] = build_question_text(record)
    record["solution_text"] = build_solution_text(record)
    return record


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def print_stats(records: list[dict]) -> None:
    total = len(records)
    print(f"\n{'='*50}")
    print(f"Total problems extracted: {total:,}")

    grade_dist: dict[str, int] = defaultdict(int)
    level_dist: dict[str, int] = defaultdict(int)
    with_tags = 0
    without_tags = 0

    for rec in records:
        grade = rec.get("grade") or "unknown"
        grade_dist[str(grade)] += 1

        school_level = rec.get("school_level") or "unknown"
        level_dist[str(school_level)] += 1

        tag_ids = rec.get("tag_ids")
        if tag_ids and str(tag_ids).strip():
            with_tags += 1
        else:
            without_tags += 1

    print(f"\nGrade distribution:")
    for grade, cnt in sorted(grade_dist.items()):
        print(f"  {grade:>10}: {cnt:>6,}  ({cnt / total * 100:.1f}%)")

    print(f"\nSchool level distribution:")
    for level, cnt in sorted(level_dist.items()):
        print(f"  {level:>10}: {cnt:>6,}  ({cnt / total * 100:.1f}%)")

    print(f"\nTag coverage:")
    print(f"  With tags   : {with_tags:>6,}  ({with_tags / total * 100:.1f}%)")
    print(f"  Without tags: {without_tags:>6,}  ({without_tags / total * 100:.1f}%)")
    print(f"{'='*50}\n")


# ---------------------------------------------------------------------------
# SQLite save
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY,
    group_id INTEGER,
    question TEXT,
    refer TEXT,
    answer TEXT,
    solution TEXT,
    choice1 TEXT,
    choice2 TEXT,
    choice3 TEXT,
    choice4 TEXT,
    choice5 TEXT,
    grade TEXT,
    school_level TEXT,
    type TEXT,
    level TEXT,
    tag_ids TEXT,
    main_category_tag_id TEXT,
    instruction TEXT,
    source_name TEXT,
    exam_type TEXT,
    year TEXT,
    school_name TEXT,
    city TEXT,
    full_text TEXT,
    question_text TEXT,
    solution_text TEXT,
    full_text_vector BLOB,
    question_vector BLOB,
    solution_vector BLOB
);
"""

INSERT_SQL = """
INSERT OR REPLACE INTO problems (
    id, group_id, question, refer, answer, solution,
    choice1, choice2, choice3, choice4, choice5,
    grade, school_level, type, level,
    tag_ids, main_category_tag_id,
    instruction, source_name, exam_type, year,
    school_name, city,
    full_text, question_text, solution_text
) VALUES (
    :id, :group_id, :question, :refer, :answer, :solution,
    :choice1, :choice2, :choice3, :choice4, :choice5,
    :grade, :school_level, :type, :level,
    :tag_ids, :main_category_tag_id,
    :instruction, :source_name, :exam_type, :year,
    :school_name, :city,
    :full_text, :question_text, :solution_text
);
"""


def save_sqlite(records: list[dict], db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()

        # Prepare rows: only include the columns we insert (no vector fields)
        rows = []
        for rec in records:
            rows.append({
                "id": rec.get("id"),
                "group_id": rec.get("group_id"),
                "question": rec.get("question"),
                "refer": rec.get("refer"),
                "answer": rec.get("answer"),
                "solution": rec.get("solution"),
                "choice1": rec.get("choice1"),
                "choice2": rec.get("choice2"),
                "choice3": rec.get("choice3"),
                "choice4": rec.get("choice4"),
                "choice5": rec.get("choice5"),
                "grade": str(rec.get("grade")) if rec.get("grade") is not None else None,
                "school_level": rec.get("school_level"),
                "type": rec.get("type"),
                "level": rec.get("level"),
                "tag_ids": rec.get("tag_ids"),
                "main_category_tag_id": rec.get("main_category_tag_id"),
                "instruction": rec.get("instruction"),
                "source_name": rec.get("source_name"),
                "exam_type": rec.get("exam_type"),
                "year": str(rec.get("year")) if rec.get("year") is not None else None,
                "school_name": rec.get("school_name"),
                "city": rec.get("city"),
                "full_text": rec.get("full_text"),
                "question_text": rec.get("question_text"),
                "solution_text": rec.get("solution_text"),
            })

        conn.executemany(INSERT_SQL, rows)
        conn.commit()
    finally:
        conn.close()
    print(f"SQLite saved → {db_path}  ({len(records):,} rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract math problems from MySQL and save to SQLite."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100_000,
        help="Maximum number of problems to extract (default: 100000)",
    )
    parser.add_argument(
        "--output",
        default="data/math_problems.db",
        help="Path to SQLite output database (default: data/math_problems.db)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Resolve output path relative to project root (one level above scripts/)
    project_root = os.path.join(os.path.dirname(__file__), "..")
    db_path = (
        args.output
        if os.path.isabs(args.output)
        else os.path.join(project_root, args.output)
    )

    print(f"Connecting to {DB_HOST}:{DB_PORT}/{DB_NAME} ...")
    print(f"Fetching up to {args.limit:,} math problems (ORDER BY RAND()) ...")

    rows = fetch_problems(args.limit)
    print(f"Fetched {len(rows):,} rows from DB.")

    print("Enriching records (clean HTML + build text fields) ...")
    records = [row_to_record(row) for row in rows]

    print_stats(records)

    save_sqlite(records, db_path)

    print("Done.")


if __name__ == "__main__":
    main()
