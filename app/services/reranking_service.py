"""LLM-based reranking service for math problem similarity.

Takes top-N candidates from vector search and re-scores them using LLM
to evaluate whether two problems use the same solving method.

Uses GPT-OSS (local) or OpenRouter (Gemini) — no GPU required.
"""

import asyncio
import html
import json
import logging
import re

import httpx
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
# Prompt
# ---------------------------------------------------------------------------

RERANK_SYSTEM_PROMPT = """당신은 두 수학 문제가 얼마나 유사한지 평가하는 전문가입니다.

두 문제의 유사도를 0~10점으로 평가해주세요.

평가 기준 (중요도 순):
1. **풀이 방법 유사도 (가장 중요)**: 같은 수학적 기법/공식/접근법을 사용하는가?
   - 예: 둘 다 근의 공식 사용, 둘 다 미분으로 극값 구하기
2. **개념 유사도**: 같은 수학 개념/단원에 속하는가?
   - 예: 둘 다 이차방정식, 둘 다 등차수열
3. **문제 구조 유사도**: 문제의 형태와 조건이 비슷한가?
   - 예: 둘 다 "~의 값을 구하시오" 형태

점수 기준:
- 9-10: 풀이 방법이 거의 동일한 문제 (쌍둥이 문제)
- 7-8: 같은 풀이 방법, 약간 다른 조건
- 5-6: 같은 개념이지만 풀이 방법이 다름
- 3-4: 관련 개념이지만 다른 문제
- 0-2: 완전히 다른 문제

반드시 아래 JSON 형식으로만 답하세요:
{"score": 7, "reason": "간단한 이유"}"""

RERANK_USER_PROMPT = """## 문제 A (기준 문제)
{query_text}

## 문제 B (비교 문제)
{candidate_text}"""


# ---------------------------------------------------------------------------
# RerankingService
# ---------------------------------------------------------------------------

class RerankingService:
    """Re-rank vector search candidates using LLM scoring."""

    def __init__(self, provider: str = "dev", concurrency: int = 16):
        self.provider = provider
        self.concurrency = concurrency
        self._client: AsyncOpenAI | None = None
        self._model: str = ""

    def _get_client(self) -> tuple[AsyncOpenAI, str]:
        """Lazy-init LLM client."""
        if self._client is not None:
            return self._client, self._model

        if self.provider == "dev":
            if not DEV_LLM_URL:
                raise ValueError("DEV_LLM_URL not set")
            self._client = AsyncOpenAI(
                base_url=DEV_LLM_URL,
                api_key=DEV_LLM_KEY or "EMPTY",
            )
            self._model = DEV_LLM_NAME or "gpt-oss-120b"
        elif self.provider == "openrouter":
            if not OPENROUTER_KEY:
                raise ValueError("OPENROUTER_API_KEY not set")
            self._client = AsyncOpenAI(
                base_url=OPENROUTER_URL,
                api_key=OPENROUTER_KEY,
                default_headers={
                    "HTTP-Referer": "https://classday.co.kr",
                    "X-Title": "math-problem-similarity",
                },
            )
            self._model = OPENROUTER_MODEL or "google/gemini-3-flash-preview"
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

        return self._client, self._model

    async def rerank(
        self,
        query_text: str,
        candidates: list[dict],
        top_k: int = 10,
    ) -> tuple[list[dict], dict]:
        """
        Re-rank candidates using LLM scoring.

        Returns:
            (results, cost_info) tuple.
        """
        empty_cost = {"cost_usd": 0, "cost_krw": 0, "num_calls": 0}
        if not candidates:
            return [], empty_cost

        client, model = self._get_client()
        sem = asyncio.Semaphore(self.concurrency)

        async def score_candidate(cand: dict) -> tuple[dict, str | None]:
            async with sem:
                cand_text = self._build_problem_text(cand)
                score, reason, gen_id = await self._score_pair(
                    client, model, query_text, cand_text
                )
                result = dict(cand)
                result["rerank_score"] = score
                result["rerank_reason"] = reason
                return result, gen_id

        tasks = [score_candidate(c) for c in candidates]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        scored = []
        gen_ids = []
        for r in raw_results:
            if isinstance(r, Exception):
                logger.error("Reranking failed for candidate: %s", r)
                continue
            result, gen_id = r
            scored.append(result)
            if gen_id:
                gen_ids.append(gen_id)

        scored.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)

        # Fetch costs from OpenRouter
        cost_usd = await self._fetch_generation_costs(gen_ids)
        cost_info = {
            "cost_usd": round(cost_usd, 6),
            "cost_krw": round(cost_usd * 1440, 2),
            "num_calls": len(gen_ids),
            "provider": self.provider,
            "model": model,
        }

        return scored[:top_k], cost_info

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags and decode entities."""
        if not text:
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _build_problem_text(cls, prob: dict) -> str:
        """Build readable problem text from a candidate dict."""
        parts = []

        question = cls._strip_html(prob.get("question", ""))
        if question:
            parts.append(f"발문: {question}")

        refer = cls._strip_html(prob.get("refer", ""))
        if refer:
            parts.append(f"보기: {refer}")

        choices = []
        for i in range(1, 6):
            c = cls._strip_html(prob.get(f"choice{i}", ""))
            if c:
                choices.append(f"{i}. {c}")
        if choices:
            parts.append("선지:\n" + "\n".join(choices))

        solution = cls._strip_html(prob.get("solution", ""))
        if solution:
            parts.append(f"해설: {solution}")

        if parts:
            return "\n".join(parts)

        # Fallback to pre-built text fields
        fallback = prob.get("solution_text", "") or prob.get("full_text", "") or ""
        return cls._strip_html(fallback)

    async def _fetch_generation_costs(self, gen_ids: list[str]) -> float:
        """Query OpenRouter generation stats for total cost (USD)."""
        if self.provider != "openrouter" or not gen_ids:
            return 0.0

        # Wait briefly for stats to propagate
        await asyncio.sleep(1)

        total = 0.0
        async with httpx.AsyncClient(timeout=10.0) as http:
            sem = asyncio.Semaphore(10)

            async def fetch_one(gid: str) -> float:
                async with sem:
                    try:
                        resp = await http.get(
                            f"{OPENROUTER_URL.rstrip('/').replace('/v1', '')}/v1/generation?id={gid}",
                            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                        )
                        data = resp.json()
                        return data.get("data", {}).get("total_cost", 0) or 0
                    except Exception:
                        return 0

            results = await asyncio.gather(*[fetch_one(gid) for gid in gen_ids])
            total = sum(r for r in results if isinstance(r, (int, float)))

        return total

    async def _score_pair(
        self,
        client: AsyncOpenAI,
        model: str,
        query_text: str,
        candidate_text: str,
    ) -> tuple[float, str, str | None]:
        """Call LLM to score similarity. Returns (score, reason, generation_id)."""
        user_prompt = RERANK_USER_PROMPT.format(
            query_text=query_text[:2000],
            candidate_text=candidate_text[:2000],
        )

        try:
            resp = await client.chat.completions.create(
                model=model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1024,
            )
            content = (resp.choices[0].message.content or "").strip()
            score, reason = self._parse_score(content)
            return score, reason, resp.id
        except Exception:
            logger.exception("LLM reranking call failed")
            return 0.0, "error", None

    @staticmethod
    def _parse_score(raw: str) -> tuple[float, str]:
        """Parse {"score": N, "reason": "..."} from LLM output."""
        if not raw:
            return 0.0, ""

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            cleaned = cleaned.strip()

        # 1) 정상 JSON 파싱
        try:
            data = json.loads(cleaned)
            score = float(data.get("score", 0))
            reason = str(data.get("reason", ""))
            return min(max(score, 0), 10), reason
        except (json.JSONDecodeError, ValueError):
            pass

        # 2) max_tokens로 잘린 불완전 JSON 복구 시도
        # 예: {"score": 7, "reason": "이유가 여기서 잘림...
        truncated = cleaned
        if not truncated.endswith("}"):
            truncated = truncated.rstrip().rstrip(",") + '"}'
            try:
                data = json.loads(truncated)
                score = float(data.get("score", 0))
                reason = str(data.get("reason", ""))
                if reason:
                    reason += "… (truncated)"
                return min(max(score, 0), 10), reason
            except (json.JSONDecodeError, ValueError):
                pass

        # 3) 정규식 Fallback — reason은 이스케이프된 따옴표 포함 가능하도록 처리
        score_match = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', cleaned)
        if score_match:
            score = float(score_match.group(1))
            # reason: "reason": "..." 에서 내용 추출 (이스케이프 따옴표 포함)
            reason_match = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned)
            if reason_match:
                reason = reason_match.group(1)
            else:
                # reason 값이 잘려서 닫히지 않은 경우도 추출
                reason_open = re.search(r'"reason"\s*:\s*"(.*)', cleaned, re.DOTALL)
                reason = (reason_open.group(1).rstrip('"}').strip() + "… (truncated)") if reason_open else ""
            return min(max(score, 0), 10), reason

        return 0.0, f"parse_error: {raw[:100]}"
