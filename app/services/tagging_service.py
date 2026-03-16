"""
Auto-tagging service for math problems.

2-stage LLM classification:
  Stage 1 (Coarse): Problem text -> matching depth2 concepts
  Stage 2 (Detail): Problem text + depth2 subtree -> depth3 // depth4 // depth5 paths
Then maps concept names to depth4_id / depth5_id via normalized string matching.

Prompts adapted from eduspace-pipelines-v2/app/services/tag_service.py
"""

import csv
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.config import (
    DEV_LLM_KEY,
    DEV_LLM_NAME,
    DEV_LLM_URL,
    OPENROUTER_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_URL,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM client factory
# ---------------------------------------------------------------------------

def get_llm_client(provider: str = "dev") -> tuple[AsyncOpenAI, str]:
    """
    Return (AsyncOpenAI client, model_name) for the given provider.

    provider: "dev" for GPT-OSS local, "openrouter" for Gemini via OpenRouter.
    """
    if provider == "dev":
        if not DEV_LLM_URL:
            raise ValueError("DEV_LLM_URL not set in environment")
        client = AsyncOpenAI(
            base_url=DEV_LLM_URL,
            api_key=DEV_LLM_KEY or "EMPTY",
        )
        model = DEV_LLM_NAME or "gpt-oss-120b"
        return client, model

    if provider == "openrouter":
        if not OPENROUTER_KEY:
            raise ValueError("OPENROUTER_API_KEY not set in environment")
        client = AsyncOpenAI(
            base_url=OPENROUTER_URL,
            api_key=OPENROUTER_KEY,
            default_headers={
                "HTTP-Referer": "https://classday.co.kr",
                "X-Title": "math-problem-similarity",
            },
        )
        model = OPENROUTER_MODEL or "google/gemini-3-flash-preview"
        return client, model

    raise ValueError(f"Unknown provider: {provider!r}  (use 'dev' or 'openrouter')")


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Normalise for fuzzy string comparison: NFKC + remove whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    return text


def _extract_paren_content(text: str) -> str | None:
    """Extract content inside parentheses: '판단1(일반급수)' -> '일반급수'."""
    if not text:
        return None
    m = re.search(r"\(([^)]+)\)", str(text))
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Prompts (adapted from eduspace-pipelines-v2/app/services/tag_service.py)
# ---------------------------------------------------------------------------

COARSE_SYSTEM_PROMPT = """당신은 수학 문제의 정보가 주어졌을 때, 아래 목록에서 문제를 풀기 위해 필요한 개념을 식별하는 전문가입니다.

- 무조건 한글로 답변합니다.
- 제공된 개념 중에서 문제를 풀기 위해 사용된 개념을 추출하세요.

주의사항
- 발문, 보기, 선지는 참고만 하고, 해설을 기반으로 개념을 판단하세요.
- 반드시 해설만 보고 판단해야 합니다.
- 아래의 개념 정보에 적힌 그대로 사용해야 합니다. 글자를 축약하거나 다른 단어를 사용하지 마세요.
- 자신의 생각으로 개념을 추가하지 말고, 주어진 개념에서만 선택하세요.
- 괄호() 안에 포함된 부분도 출력에 포함하세요.
- 아래 목록에서 정답을 도출하는 데 사용된 핵심 개념만 출력하세요.
- 아래 목록에 해당하는 핵심 개념이 없으면 빈 배열을 출력하세요.

추가 규칙
- "~~ 구하기"는 해설에 나타나는 개념이 아니라, 문제의 실제 정답이 "~~을 구하는 것"일 때만 사용합니다.
- 부등호(<, >)는 부호 개념의 기호가 아닙니다.
- "~~ 과정" 형태의 개념은 해설에서 정답을 구하는 과정 속에 포함된 개념입니다.
- "~~ 조건" 형태의 개념은 발문에서 제시된 조건만 사용 가능합니다.

## 개념 정보
{merge}

### 출력
{{
  "using_concepts": ["concept1", ...]
}}"""

DETAIL_SYSTEM_PROMPT = """당신은 수학 문제의 정보가 주어졌을 때, 아래 목록에서 문제를 풀기 위해 필요한 구체적인 개념을 식별하는 전문가입니다.

- 무조건 한글로 답변합니다.
- 제공된 개념 중에서 문제를 풀기 위해 사용된 개념을 추출하세요.
- 제공된 개념은 3단계 구조로 되어 있습니다: depth3 – depth4 – depth5.
- 출력 형식은 '{{depth3}} // {{depth4}} // {{depth5}}'입니다.

주의사항
- 발문, 보기, 선지는 참고만 하고, 해설을 기반으로 개념을 판단하세요.
- 반드시 해설만 보고 판단해야 합니다.
- 아래의 개념 정보에 적힌 그대로 사용해야 합니다. 글자를 축약하거나 다른 단어를 사용하지 마세요.
- 자신의 생각으로 개념을 추가하지 말고, 주어진 개념에서만 선택하세요.
- 괄호() 안에 포함된 부분도 출력에 포함하세요.
- 아래 목록에서 정답을 도출하는 데 사용된 핵심 개념만 출력하세요.
- 아래 목록에 해당하는 핵심 개념이 없으면 빈 배열을 출력하세요.
- 각 depth 구성요소는 반드시 "depth3 // depth4 // depth5" 형식을 따라야 하며, 해당 depth3 아래에 있는 depth4를 사용하고, 해당 depth4 아래에 있는 depth5만 사용해야 합니다.

추가 규칙
- "~~ 구하기"는 해설에 나타나는 개념이 아니라, 문제의 실제 정답이 "~~을 구하는 것"일 때만 사용합니다.
- 부등호(<, >)는 부호 개념의 기호가 아닙니다.
- "~~ 과정" 형태의 개념은 해설에서 정답을 구하는 과정 속에 포함된 개념입니다.
- "~~ 조건" 형태의 개념은 발문에서 제시된 조건만 사용 가능합니다.

## 개념 정보
{merge}

### 출력
{{
  "using_concepts": ["depth3 // depth4 // depth5", "…"]
}}"""


# ---------------------------------------------------------------------------
# TaggingService
# ---------------------------------------------------------------------------

class TaggingService:
    """Load tag hierarchies from CSV and run 2-stage LLM classification.

    Stage 1: Coarse — classify into depth2 (e.g. "다항식의 연산", "등차수열")
    Stage 2: Detail — within matched depth2s, pick specific depth3 // depth4 // depth5
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}  # keyed by csv path

    # ------------------------------------------------------------------
    # Tag loading
    # ------------------------------------------------------------------

    def load_tags(self, csv_path: str) -> dict:
        """
        Parse a tag CSV and return a structured hierarchy dict.

        Returns:
            {
                "depth2_list": [str, ...],
                "hierarchy": {
                    depth2_name: {
                        "depth3": {
                            depth3_name: {
                                "depth4": {
                                    depth4_name: {
                                        "id": int,
                                        "depth5": {depth5_name: int, ...}
                                    }
                                }
                            }
                        }
                    }
                },
                "rows": [dict, ...],
                "d4_by_id": {int: dict},
                "d5_by_id": {int: dict},
                "d4_name_to_id": {normalised_name: int},
                "d5_name_to_id": {normalised_name: int},
            }
        """
        if csv_path in self._cache:
            return self._cache[csv_path]

        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Tag CSV not found: {csv_path}")

        rows: list[dict] = []
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row["depth4_id"] = int(row["depth4_id"])
                row["depth5_id"] = int(row["depth5_id"])
                rows.append(row)

        # Build hierarchy -------------------------------------------------
        hierarchy: dict[str, Any] = {}
        depth2_set: set[str] = set()
        d4_by_id: dict[int, dict] = {}
        d5_by_id: dict[int, dict] = {}
        d4_name_to_id: dict[str, int] = {}
        d5_name_to_id: dict[str, int] = {}

        for r in rows:
            d2 = r["depth2"]
            d3 = r["depth3"]
            d4 = r["depth4"]
            d5 = r["depth5"]
            d4_id = r["depth4_id"]
            d5_id = r["depth5_id"]

            depth2_set.add(d2)

            # hierarchy[d2]["depth3"][d3]["depth4"][d4] = {"id": ..., "depth5": {d5: id}}
            hierarchy.setdefault(d2, {"depth3": {}})
            d3_map = hierarchy[d2]["depth3"]
            d3_map.setdefault(d3, {"depth4": {}})
            d4_map = d3_map[d3]["depth4"]
            d4_map.setdefault(d4, {"id": d4_id, "depth5": {}})
            d4_map[d4]["depth5"][d5] = d5_id

            d4_by_id[d4_id] = r
            d5_by_id[d5_id] = r
            d4_name_to_id[_normalise(d4)] = d4_id
            d5_name_to_id[_normalise(d5)] = d5_id

        result = {
            "depth2_list": sorted(depth2_set),
            "hierarchy": hierarchy,
            "rows": rows,
            "d4_by_id": d4_by_id,
            "d5_by_id": d5_by_id,
            "d4_name_to_id": d4_name_to_id,
            "d5_name_to_id": d5_name_to_id,
        }
        self._cache[csv_path] = result
        return result

    # ------------------------------------------------------------------
    # Stage 1: Coarse — classify into depth2
    # ------------------------------------------------------------------

    async def classify_coarse(
        self,
        problem_text: str,
        depth2_list: list[str],
        client: AsyncOpenAI,
        model: str,
    ) -> list[str]:
        """
        Ask LLM which depth2 concepts are relevant to the problem.

        Returns list of matched depth2 concept names (subset of depth2_list).
        """
        concept_bullets = "\n".join(f"- {c}" for c in depth2_list)
        system_prompt = COARSE_SYSTEM_PROMPT.replace("{merge}", concept_bullets)

        user_prompt = (
            "다음 수학 문제를 분석하고, 풀이에 사용된 개념을 위 목록에서 골라주세요.\n\n"
            f"{problem_text}"
        )

        raw = await self._call_llm(client, model, system_prompt, user_prompt)
        parsed = self._parse_json_response(raw)
        concepts = parsed.get("using_concepts", [])
        if not isinstance(concepts, list):
            logger.warning("Coarse: using_concepts is not a list: %s", concepts)
            return []

        # Filter to only valid depth2 names (exact match)
        valid = set(depth2_list)
        matched = [c for c in concepts if c in valid]

        # Fuzzy fallback: if LLM returned a name that's close but not exact
        if not matched and concepts:
            norm_map = {_normalise(d2): d2 for d2 in depth2_list}
            for c in concepts:
                norm_c = _normalise(c)
                if norm_c in norm_map:
                    matched.append(norm_map[norm_c])
                else:
                    for norm_d2, orig_d2 in norm_map.items():
                        if norm_c in norm_d2 or norm_d2 in norm_c:
                            matched.append(orig_d2)
                            break

        return list(dict.fromkeys(matched))  # deduplicate

    # ------------------------------------------------------------------
    # Stage 2: Detail — pick depth3 // depth4 // depth5 under matched depth2s
    # ------------------------------------------------------------------

    async def classify_detail(
        self,
        problem_text: str,
        matched_depth2s: list[str],
        hierarchy: dict,
        client: AsyncOpenAI,
        model: str,
    ) -> list[str]:
        """
        For each matched depth2, build its depth3->depth4->depth5 subtree
        and ask the LLM to pick specific paths.

        Returns list of "depth3 // depth4 // depth5" strings.
        """
        if not matched_depth2s:
            return []

        # Build a readable tree showing depth3 -> depth4 -> depth5
        tree_lines: list[str] = []
        for d2 in matched_depth2s:
            node = hierarchy.get(d2)
            if not node:
                continue
            tree_lines.append(f"[{d2}]")
            for d3_name, d3_node in node["depth3"].items():
                tree_lines.append(f"  - {d3_name}")
                for d4_name, d4_node in d3_node["depth4"].items():
                    d5_names = ", ".join(d4_node["depth5"].keys())
                    tree_lines.append(f"      - {d4_name}")
                    tree_lines.append(f"          - {d5_names}")

        tree_text = "\n".join(tree_lines)
        system_prompt = DETAIL_SYSTEM_PROMPT.replace("{merge}", tree_text)

        user_prompt = (
            "다음 수학 문제의 풀이(해설)를 분석하고, 사용된 구체적인 개념을 "
            "위 계층 구조에서 골라 지정된 형식으로 출력하세요.\n\n"
            f"{problem_text}"
        )

        raw = await self._call_llm(client, model, system_prompt, user_prompt)
        parsed = self._parse_json_response(raw)
        concepts = parsed.get("using_concepts", [])
        if not isinstance(concepts, list):
            logger.warning("Detail: using_concepts is not a list: %s", concepts)
            return []
        return concepts

    # ------------------------------------------------------------------
    # Concept-to-ID mapping
    # ------------------------------------------------------------------

    def map_concepts_to_ids(
        self, concept_paths: list[str], tags: dict
    ) -> list[dict]:
        """
        Map "depth3 // depth4 // depth5" strings to depth4_id / depth5_id.

        Also handles "depth4 // depth5" (2-part) format.

        Returns list of {
            "path": str,
            "depth4_id": int | None,
            "depth5_id": int | None,
        }
        """
        results: list[dict] = []
        hierarchy = tags["hierarchy"]
        d4_name_to_id = tags["d4_name_to_id"]
        d5_name_to_id = tags["d5_name_to_id"]

        for path_str in concept_paths:
            parts = [p.strip() for p in path_str.split("//")]
            if len(parts) < 2:
                logger.warning("Skipping malformed path (need >=2 parts): %s", path_str)
                results.append({"path": path_str, "depth4_id": None, "depth5_id": None})
                continue

            # Support both "d3 // d4 // d5" and "d4 // d5" formats
            if len(parts) >= 3:
                d3_name, d4_name, d5_name = parts[0], parts[1], parts[2]
            else:
                d3_name, d4_name, d5_name = None, parts[0], parts[1]

            # Try normalised name lookup
            d4_id = d4_name_to_id.get(_normalise(d4_name))
            d5_id = d5_name_to_id.get(_normalise(d5_name))

            # Fallback: exact hierarchy walk
            if d4_id is None or d5_id is None:
                found_d4, found_d5 = self._find_in_hierarchy(
                    hierarchy, d3_name, d4_name, d5_name
                )
                if d4_id is None:
                    d4_id = found_d4
                if d5_id is None:
                    d5_id = found_d5

            # Fuzzy fallback
            if d4_id is None:
                d4_id = self._fuzzy_match(d4_name, d4_name_to_id)
            if d5_id is None:
                d5_id = self._fuzzy_match(d5_name, d5_name_to_id)

            # Derive depth4_id from depth5's parent row
            if d4_id is None and d5_id is not None:
                d5_row = tags["d5_by_id"].get(d5_id)
                if d5_row:
                    d4_id = d5_row.get("depth4_id")

            results.append({
                "path": path_str,
                "depth4_id": d4_id,
                "depth5_id": d5_id,
            })

        return results

    # ------------------------------------------------------------------
    # Full pipeline: problem_text -> tag IDs
    # ------------------------------------------------------------------

    async def tag_problem(
        self,
        problem_text: str,
        school_level: str,
        client: AsyncOpenAI,
        model: str,
        tags_dir: str | None = None,
    ) -> dict:
        """
        Run the full 2-stage pipeline on a single problem.

        Stage 1: Coarse — depth2 classification
        Stage 2: Detail — depth3 // depth4 // depth5 under matched depth2s

        Returns {
            "depth4_ids": [int, ...],
            "depth5_ids": [int, ...],
            "tag_names": [str, ...],      # human-readable paths
        }
        """
        if tags_dir is None:
            tags_dir = str(Path(__file__).parent.parent.parent / "data" / "tags")

        csv_name = "tag_high.csv" if school_level == "high" else "tag_middle.csv"
        csv_path = str(Path(tags_dir) / csv_name)
        tags = self.load_tags(csv_path)

        # Stage 1: coarse — depth2
        matched_d2 = await self.classify_coarse(
            problem_text, tags["depth2_list"], client, model
        )
        if not matched_d2:
            logger.debug("No depth2 concepts matched for problem")
            return {"depth4_ids": [], "depth5_ids": [], "tag_names": []}

        # Stage 2: detail — depth3 // depth4 // depth5 under matched depth2s
        concept_paths = await self.classify_detail(
            problem_text, matched_d2, tags["hierarchy"], client, model
        )
        if not concept_paths:
            logger.debug("No detail concepts returned for problem")
            return {"depth4_ids": [], "depth5_ids": [], "tag_names": []}

        # Map to IDs
        mapped = self.map_concepts_to_ids(concept_paths, tags)

        depth4_ids = [m["depth4_id"] for m in mapped if m["depth4_id"] is not None]
        depth5_ids = [m["depth5_id"] for m in mapped if m["depth5_id"] is not None]
        tag_names = [m["path"] for m in mapped]

        # Deduplicate while preserving order
        depth4_ids = list(dict.fromkeys(depth4_ids))
        depth5_ids = list(dict.fromkeys(depth5_ids))
        tag_names = list(dict.fromkeys(tag_names))

        return {
            "depth4_ids": depth4_ids,
            "depth5_ids": depth5_ids,
            "tag_names": tag_names,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        client: AsyncOpenAI,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Call LLM and return raw content string."""
        try:
            resp = await client.chat.completions.create(
                model=model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2048,
            )
            content = resp.choices[0].message.content or ""
            return content.strip()
        except Exception:
            logger.exception("LLM call failed")
            return ""

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """Parse JSON from LLM output, handling code blocks and broken JSON."""
        if not raw:
            return {}

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            cleaned = cleaned.strip()

        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[^{}]*\"using_concepts\"\s*:\s*\[.*?\]\s*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse JSON from LLM response: %s", raw[:300])
        return {}

    @staticmethod
    def _find_in_hierarchy(
        hierarchy: dict, d3_name: str | None, d4_name: str, d5_name: str
    ) -> tuple[int | None, int | None]:
        """Walk hierarchy to find depth4 ID and depth5 ID by name."""
        d4_id = None
        d5_id = None
        for d2_node in hierarchy.values():
            d3_map = d2_node.get("depth3", {})
            # If d3_name given, try exact match first
            if d3_name:
                d3_node = d3_map.get(d3_name)
                if d3_node:
                    d4_node = d3_node.get("depth4", {}).get(d4_name)
                    if d4_node:
                        d4_id = d4_node["id"]
                        d5_val = d4_node["depth5"].get(d5_name)
                        if d5_val is not None:
                            return d4_id, d5_val
            # Scan all depth3s
            for d3_node in d3_map.values():
                d4_node = d3_node.get("depth4", {}).get(d4_name)
                if d4_node:
                    d4_id = d4_node["id"]
                    d5_val = d4_node["depth5"].get(d5_name)
                    if d5_val is not None:
                        return d4_id, d5_val
        return d4_id, d5_id

    @staticmethod
    def _fuzzy_match(name: str, name_to_id: dict[str, int]) -> int | None:
        """Find closest match by normalised substring containment."""
        norm = _normalise(name)
        if not norm:
            return None

        if norm in name_to_id:
            return name_to_id[norm]

        # Match against parenthetical content
        paren = _extract_paren_content(name)
        if paren:
            paren_norm = _normalise(paren)
            if paren_norm in name_to_id:
                return name_to_id[paren_norm]

        # Substring matching
        candidates = []
        for cand_norm, cand_id in name_to_id.items():
            if norm in cand_norm or cand_norm in norm:
                candidates.append((cand_norm, cand_id))

        if len(candidates) == 1:
            return candidates[0][1]

        if candidates:
            best = min(candidates, key=lambda c: abs(len(c[0]) - len(norm)))
            return best[1]

        return None
