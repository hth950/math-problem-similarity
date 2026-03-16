#!/usr/bin/env python3
"""
Generate split embeddings for math problems.

Reads problems from SQLite (data/math_problems.db), generates:
- full_text_vector: embed(question + refer + choices + solution) — legacy single vector
- question_vector: embed(question + refer + choices) — problem structure
- solution_vector: embed(solution) — solving method

Updates the problems table in the SQLite DB with BLOB vectors.

Supports --resume to skip problems where vectors already exist.
"""
import sys, os, sqlite3, asyncio, argparse, re, html
import numpy as np
from pathlib import Path
from tqdm.asyncio import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import AsyncOpenAI, RateLimitError
from app.config import OPENAI_API_KEY_EMBEDDING
from bs4 import BeautifulSoup

EMB_MODEL = "text-embedding-3-large"
BATCH_SIZE = 50  # OpenAI batch limit

client = AsyncOpenAI(api_key=OPENAI_API_KEY_EMBEDDING)

# HTML cleaning (same as extract_problems.py)
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


def make_question_text(prob: dict) -> str:
    """Question + refer + choices (without solution)."""
    parts = [f"발문: {clean_html(prob.get('question', ''))}"]
    if prob.get('refer'):
        ref = clean_html(prob['refer'])
        if ref:
            parts.append(f"보기: {ref}")
    choices = [
        clean_html(prob.get(f'choice{i}', ''))
        for i in range(1, 6)
        if prob.get(f'choice{i}')
    ]
    if choices:
        parts.append("선지:")
        parts.extend(f"{i}. {c}" for i, c in enumerate(choices, 1))
    return "\n".join(parts)


def make_solution_text(prob: dict) -> str:
    """Solution text only."""
    return clean_html(prob.get('solution', ''))


def make_full_text(prob: dict) -> str:
    """Full combined text (legacy format)."""
    return f"{make_question_text(prob)}\n해설: {make_solution_text(prob)}"


def embedding_to_bytes(embedding: list[float]) -> bytes:
    """Convert embedding list to numpy float32 bytes for BLOB storage."""
    return np.array(embedding, dtype=np.float32).tobytes()


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with rate limit handling."""
    results = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        # Filter out empty texts
        batch = [t if t.strip() else "빈 텍스트" for t in batch]
        for attempt in range(3):
            try:
                resp = await client.embeddings.create(model=EMB_MODEL, input=batch)
                results.extend([d.embedding for d in resp.data])
                break
            except RateLimitError:
                wait = 5 * (attempt + 1)
                print(f"Rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
            except Exception as e:
                print(f"Error embedding batch: {e}")
                # Try individual items
                for text in batch:
                    try:
                        resp = await client.embeddings.create(model=EMB_MODEL, input=[text])
                        results.append(resp.data[0].embedding)
                    except Exception:
                        results.append([0.0] * 3072)  # zero vector as fallback
                break
    return results


def load_problems_from_db(db_path: str, resume: bool) -> list[dict]:
    """Load problems from SQLite. If resume=True, skip rows with existing vectors."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if resume:
            cursor = conn.execute(
                "SELECT * FROM problems WHERE full_text_vector IS NULL"
            )
        else:
            cursor = conn.execute("SELECT * FROM problems")
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return rows


def save_vectors_batch(db_path: str, updates: list[tuple]) -> None:
    """
    Persist a batch of vector updates to SQLite.

    updates: list of (full_text_vector_bytes, question_vector_bytes, solution_vector_bytes, problem_id)
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            UPDATE problems
            SET full_text_vector = ?, question_vector = ?, solution_vector = ?
            WHERE id = ?
            """,
            updates,
        )
        conn.commit()
    finally:
        conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Generate split embeddings for math problems")
    parser.add_argument("--db", default="data/math_problems.db",
                        help="Path to SQLite database (default: data/math_problems.db)")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None, help="Process only first N problems")
    parser.add_argument("--resume", action="store_true",
                        help="Skip problems where vectors already exist")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = Path(__file__).parent.parent / db_path

    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        print("Run scripts/extract_problems.py first.")
        sys.exit(1)

    print(f"Loading problems from {db_path}...")
    if args.resume:
        print("Resume mode: skipping problems with existing vectors.")

    problems = load_problems_from_db(str(db_path), resume=args.resume)

    if args.limit:
        problems = problems[:args.limit]

    total = len(problems)
    if total == 0:
        print("No problems to process (all vectors already exist, or DB is empty).")
        return

    print(f"Generating embeddings for {total} problems...")

    # Process in batches, checkpointing after each batch
    batch_size = args.batch_size
    processed = 0

    for batch_start in range(0, total, batch_size):
        batch = problems[batch_start:batch_start + batch_size]
        batch_ids = [p["id"] for p in batch]

        question_texts = [make_question_text(p) for p in batch]
        solution_texts = [make_solution_text(p) for p in batch]
        full_texts = [make_full_text(p) for p in batch]

        # Generate all three vector types for this batch
        question_vectors = await embed_batch(question_texts)
        solution_vectors = await embed_batch(solution_texts)
        full_vectors = await embed_batch(full_texts)

        # Build update tuples: (full_bytes, question_bytes, solution_bytes, id)
        updates = []
        for i, prob_id in enumerate(batch_ids):
            updates.append((
                embedding_to_bytes(full_vectors[i]),
                embedding_to_bytes(question_vectors[i]),
                embedding_to_bytes(solution_vectors[i]),
                prob_id,
            ))

        # Checkpoint: save immediately after each batch
        save_vectors_batch(str(db_path), updates)
        processed += len(batch)
        print(f"  Checkpoint saved: {processed}/{total} problems ({processed/total*100:.1f}%)")

    print(f"Done. {processed} problems updated with vectors in {db_path}")


if __name__ == "__main__":
    asyncio.run(main())
