#!/usr/bin/env python3
"""
Auto-tag math problems using 2-stage LLM classification.

Reads untagged problems from SQLite, classifies them via LLM into the
tag hierarchy (depth4/depth5), and writes results back to the DB.

Usage:
    python scripts/auto_tag_problems.py --limit 10 --provider dev
    python scripts/auto_tag_problems.py --resume --batch-size 20 --concurrency 5
    python scripts/auto_tag_problems.py --provider openrouter --limit 100
"""

import argparse
import asyncio
import html
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from tqdm import tqdm

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.tagging_service import TaggingService, get_llm_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "auto_tag.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("auto_tag")

# ---------------------------------------------------------------------------
# HTML cleaning (same as generate_embeddings.py)
# ---------------------------------------------------------------------------

TABLE_ATTRS = {"style", "width", "height", "cellpadding", "cellspacing", "border"}


def clean_html(raw: str) -> str:
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
# Problem text builders (same format as generate_embeddings.py)
# ---------------------------------------------------------------------------


def make_problem_text(prob: dict) -> str:
    """
    Build the full problem text for LLM classification.

    Format:
        발문: {question}
        보기: {refer}
        선지:
        1. {choice1}
        ...
        해설: {solution}
    """
    parts = [f"발문: {clean_html(prob.get('question', ''))}"]

    if prob.get("refer"):
        ref = clean_html(prob["refer"])
        if ref:
            parts.append(f"보기: {ref}")

    choices = [
        clean_html(prob.get(f"choice{i}", ""))
        for i in range(1, 6)
        if prob.get(f"choice{i}")
    ]
    if choices:
        parts.append("선지:")
        parts.extend(f"{i}. {c}" for i, c in enumerate(choices, 1))

    sol = clean_html(prob.get("solution", ""))
    if sol:
        parts.append(f"해설: {sol}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "math_problems.db"
TAGS_DIR = PROJECT_ROOT / "data" / "tags"


def ensure_columns(db_path: str) -> None:
    """Add auto_tag columns to the problems table if they don't exist."""
    conn = sqlite3.connect(db_path)
    try:
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(problems)")
        }
        new_cols = [
            ("auto_tag_ids", "TEXT"),
            ("auto_tag_depth5_ids", "TEXT"),
            ("auto_tag_names", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing:
                conn.execute(
                    f"ALTER TABLE problems ADD COLUMN {col_name} {col_type}"
                )
                logger.info("Added column: %s %s", col_name, col_type)
        conn.commit()
    finally:
        conn.close()


def load_untagged_problems(
    db_path: str,
    resume: bool,
    school_levels: list[str] | None = None,
) -> list[dict]:
    """
    Load problems that need auto-tagging.

    If resume=True, skip problems where auto_tag_ids is already set (non-NULL and non-empty).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        levels = school_levels or ["high", "middle"]
        placeholders = ",".join("?" for _ in levels)

        if resume:
            query = (
                f"SELECT * FROM problems "
                f"WHERE school_level IN ({placeholders}) "
                f"AND (auto_tag_ids IS NULL OR auto_tag_ids = '')"
            )
        else:
            query = (
                f"SELECT * FROM problems "
                f"WHERE school_level IN ({placeholders})"
            )

        rows = [dict(row) for row in conn.execute(query, levels).fetchall()]
    finally:
        conn.close()
    return rows


def save_tag_results(db_path: str, updates: list[tuple]) -> None:
    """
    Persist tag results to SQLite.

    updates: list of (auto_tag_ids, auto_tag_depth5_ids, auto_tag_names, problem_id)
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            UPDATE problems
            SET auto_tag_ids = ?,
                auto_tag_depth5_ids = ?,
                auto_tag_names = ?
            WHERE id = ?
            """,
            updates,
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Async processing
# ---------------------------------------------------------------------------


async def process_problem(
    prob: dict,
    service: TaggingService,
    client,
    model: str,
    semaphore: asyncio.Semaphore,
    tags_dir: str,
) -> tuple[int, str, str, str]:
    """
    Process a single problem through the 2-stage pipeline.

    Returns (problem_id, auto_tag_ids, auto_tag_depth5_ids, auto_tag_names).
    """
    prob_id = prob["id"]
    school_level = prob.get("school_level", "high")

    async with semaphore:
        try:
            problem_text = make_problem_text(prob)
            if not problem_text.strip():
                logger.warning("Problem %s has empty text, skipping", prob_id)
                return (prob_id, "", "", "")

            result = await service.tag_problem(
                problem_text=problem_text,
                school_level=school_level,
                client=client,
                model=model,
                tags_dir=tags_dir,
            )

            d4_ids_str = ",".join(str(x) for x in result["depth4_ids"])
            d5_ids_str = ",".join(str(x) for x in result["depth5_ids"])
            names_str = " | ".join(result["tag_names"])

            return (prob_id, d4_ids_str, d5_ids_str, names_str)

        except Exception:
            logger.exception("Error processing problem %s", prob_id)
            return (prob_id, "", "", "")


async def process_batch(
    batch: list[dict],
    service: TaggingService,
    client,
    model: str,
    semaphore: asyncio.Semaphore,
    tags_dir: str,
) -> list[tuple]:
    """
    Process a batch of problems concurrently (up to semaphore limit).

    Returns list of (auto_tag_ids, auto_tag_depth5_ids, auto_tag_names, problem_id)
    tuples ready for DB update.
    """
    tasks = [
        process_problem(prob, service, client, model, semaphore, tags_dir)
        for prob in batch
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    updates = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("Batch task failed: %s", r)
            continue
        prob_id, d4_str, d5_str, names_str = r
        # DB update format: (auto_tag_ids, auto_tag_depth5_ids, auto_tag_names, id)
        updates.append((d4_str, d5_str, names_str, prob_id))

    return updates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(
        description="Auto-tag math problems using 2-stage LLM classification"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N problems (for testing)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Problems per checkpoint batch (default: 10)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max concurrent LLM calls (default: 5)",
    )
    parser.add_argument(
        "--provider",
        choices=["dev", "openrouter"],
        default="dev",
        help="LLM provider: 'dev' for GPT-OSS, 'openrouter' for Gemini (default: dev)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip problems that already have auto_tag_ids",
    )
    parser.add_argument(
        "--school-level",
        choices=["high", "middle", "both"],
        default="both",
        help="Which school level to process (default: both)",
    )
    args = parser.parse_args()

    # Resolve DB path
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    db_path_str = str(db_path)

    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        sys.exit(1)

    # Ensure log directory exists
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    # Ensure auto_tag columns exist
    ensure_columns(db_path_str)

    # Determine school levels
    if args.school_level == "both":
        school_levels = ["high", "middle"]
    else:
        school_levels = [args.school_level]

    # Load problems
    logger.info("Loading problems from %s (resume=%s)...", db_path, args.resume)
    problems = load_untagged_problems(
        db_path_str, resume=args.resume, school_levels=school_levels
    )

    if args.limit:
        problems = problems[: args.limit]

    total = len(problems)
    if total == 0:
        logger.info("No problems to process.")
        return

    logger.info(
        "Processing %d problems (batch_size=%d, concurrency=%d, provider=%s)",
        total,
        args.batch_size,
        args.concurrency,
        args.provider,
    )

    # Init LLM client
    client, model = get_llm_client(args.provider)
    logger.info("Using LLM: %s (model=%s)", args.provider, model)

    # Init service
    service = TaggingService()
    semaphore = asyncio.Semaphore(args.concurrency)
    tags_dir_str = str(TAGS_DIR)

    # Process in batches with checkpointing
    batch_size = args.batch_size
    processed = 0
    tagged_count = 0
    error_count = 0
    start_time = time.time()

    pbar = tqdm(total=total, desc="Auto-tagging", unit="prob")

    for batch_start in range(0, total, batch_size):
        batch = problems[batch_start : batch_start + batch_size]

        updates = await process_batch(
            batch, service, client, model, semaphore, tags_dir_str
        )

        # Checkpoint: save immediately
        if updates:
            save_tag_results(db_path_str, updates)

        # Count results (either d4 or d5 IDs = success)
        for d4_str, d5_str, _names_str, _pid in updates:
            if d4_str or d5_str:
                tagged_count += 1
            else:
                error_count += 1

        processed += len(batch)
        pbar.update(len(batch))

        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        pbar.set_postfix(
            tagged=tagged_count,
            errors=error_count,
            rate=f"{rate:.1f}/s",
        )

    pbar.close()

    elapsed = time.time() - start_time
    logger.info(
        "Done. Processed %d problems in %.1fs (%.1f/s). "
        "Tagged: %d, Errors/Empty: %d",
        processed,
        elapsed,
        processed / elapsed if elapsed > 0 else 0,
        tagged_count,
        error_count,
    )


if __name__ == "__main__":
    asyncio.run(main())
