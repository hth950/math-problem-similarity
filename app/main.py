import asyncio

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from app.db.mysql_client import MySQLClient
from app.db.neo4j_client import Neo4jClient
from app.services.search_service import SearchService
from app.services.reranking_service import RerankingService
from app.services.graph_search_service import GraphSearchService

app = FastAPI(title="Math Problem Similarity A/B Comparison")

# Static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Services
db = MySQLClient()
search = SearchService()
reranker = RerankingService(provider="dev", concurrency=16)
neo4j_client = Neo4jClient()
graph_search = GraphSearchService(neo4j_client)
_eval_lock = asyncio.Lock()


@app.on_event("startup")
async def startup():
    db_path = Path(__file__).parent.parent / "data" / "math_problems.db"
    search.load_problems(str(db_path))
    try:
        neo4j_client.connect()
        graph_search.set_sqlite_path(str(db_path))
        print("Neo4j connected for graph search.")
    except Exception as e:
        print(f"Neo4j not available (graph search disabled): {e}")


@app.on_event("shutdown")
async def shutdown():
    neo4j_client.close()


# Pydantic models
class SearchRequest(BaseModel):
    question: str = ""
    solution: str = ""
    top_k: int = 10
    q_weight: float = 0.3
    s_weight: float = 0.7
    grade: int | None = None
    school_level: str | None = None
    exclude_id: int | None = None
    rerank: bool = False
    rerank_top_k: int = 30  # candidates to fetch before reranking
    rerank_provider: str = "dev"  # "dev" or "openrouter"
    problem_id: int | None = None
    graph_enabled: bool = False
    graph_alpha: float = 0.6
    graph_beta: float = 0.4


class EvaluationRequest(BaseModel):
    query_problem_id: int | None = None
    result_problem_id: int
    is_similar: bool
    search_type: str  # "legacy", "improved", "reranked", or "graph"


# Routes
@app.get("/", response_class=HTMLResponse)
async def index():
    return (static_dir / "index.html").read_text()


@app.get("/api/problem/random")
async def get_random_problem():
    import sqlite3
    db_path = Path(__file__).parent.parent / "data" / "math_problems.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, group_id, question, refer, answer, solution, "
            "choice1, choice2, choice3, choice4, choice5, "
            "grade, school_level, type, level, tag_ids, main_category_tag_id, "
            "instruction, source_name, exam_type, year, school_name, city, "
            "full_text, question_text, solution_text "
            "FROM problems ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if not row:
            return {"error": "No problems in database"}
        return dict(row)
    finally:
        conn.close()


@app.get("/api/problem/{problem_id}")
async def get_problem(problem_id: int):
    problem = db.fetch_problem_by_id(problem_id)
    if not problem:
        return {"error": "Problem not found"}
    return problem


@app.post("/api/search/legacy")
async def search_legacy(req: SearchRequest):
    full_text = f"{req.question}\n{req.solution}" if req.solution else req.question
    results = await search.search_legacy(full_text, req.top_k, req.grade, req.school_level, req.exclude_id)
    return {"results": results}


@app.post("/api/search/improved")
async def search_improved(req: SearchRequest):
    if req.rerank:
        # Fetch more candidates, then rerank with LLM
        candidates = await search.search_improved(
            req.question, req.solution, req.rerank_top_k,
            req.q_weight, req.s_weight, req.grade, req.school_level, req.exclude_id
        )
        query_text = f"발문: {req.question}\n해설: {req.solution}"
        if reranker.provider != req.rerank_provider:
            reranker.provider = req.rerank_provider
            reranker._client = None  # reset client
        results, cost_info = await reranker.rerank(query_text, candidates, req.top_k)
        return {"results": results, "reranked": True, "candidates_before_rerank": len(candidates), "cost_info": cost_info}
    results = await search.search_improved(
        req.question, req.solution, req.top_k,
        req.q_weight, req.s_weight, req.grade, req.school_level, req.exclude_id
    )
    return {"results": results}


@app.post("/api/search/rerank")
async def search_rerank(req: SearchRequest):
    """Vector search (improved) + LLM reranking pipeline."""
    # Step 1: vector search for top-N candidates
    candidates = await search.search_improved(
        req.question, req.solution, req.rerank_top_k,
        req.q_weight, req.s_weight, req.grade, req.school_level, req.exclude_id
    )
    # Step 2: LLM reranking
    query_text = f"발문: {req.question}\n해설: {req.solution}"
    if reranker.provider != req.rerank_provider:
        reranker.provider = req.rerank_provider
        reranker._client = None
    results, cost_info = await reranker.rerank(query_text, candidates, req.top_k)
    return {
        "results": results,
        "reranked": True,
        "candidates_before_rerank": len(candidates),
        "cost_info": cost_info,
    }


@app.post("/api/search/graph")
async def search_graph(req: SearchRequest):
    """Graph-based hybrid search: Neo4j vector + tag structure."""
    if not neo4j_client.driver:
        return {"error": "Neo4j not connected", "results": []}
    results = await graph_search.search_hybrid(
        problem_id=req.problem_id,
        question=req.question,
        solution=req.solution,
        top_k=req.top_k,
        alpha=req.graph_alpha,
        beta=req.graph_beta,
        grade=req.grade,
        school_level=req.school_level,
        exclude_id=req.exclude_id,
    )
    return {"results": results}


@app.post("/api/search/compare")
async def search_compare(req: SearchRequest):
    full_text = f"{req.question}\n{req.solution}"

    # Build task list
    tasks = {
        "legacy": search.search_legacy(
            full_text, req.top_k, req.grade, req.school_level, req.exclude_id
        ),
        "improved": search.search_improved(
            req.question, req.solution, req.top_k,
            req.q_weight, req.s_weight, req.grade, req.school_level, req.exclude_id
        ),
    }

    if req.rerank:
        tasks["rerank_candidates"] = search.search_improved(
            req.question, req.solution, req.rerank_top_k,
            req.q_weight, req.s_weight, req.grade, req.school_level, req.exclude_id
        )

    if req.graph_enabled and neo4j_client.driver:
        tasks["graph"] = graph_search.search_hybrid(
            problem_id=req.problem_id,
            question=req.question,
            solution=req.solution,
            top_k=req.top_k,
            alpha=req.graph_alpha,
            beta=req.graph_beta,
            grade=req.grade,
            school_level=req.school_level,
            exclude_id=req.exclude_id,
        )

    # Execute all in parallel
    keys = list(tasks.keys())
    results_list = await asyncio.gather(*tasks.values())
    results_map = dict(zip(keys, results_list))

    response = {
        "legacy": results_map["legacy"],
        "improved": results_map["improved"],
    }

    if req.rerank:
        query_text = f"발문: {req.question}\n해설: {req.solution}"
        if reranker.provider != req.rerank_provider:
            reranker.provider = req.rerank_provider
            reranker._client = None
        reranked_results, cost_info = await reranker.rerank(
            query_text, results_map["rerank_candidates"], req.top_k
        )
        response["reranked"] = reranked_results
        response["cost_info"] = cost_info

    if "graph" in results_map:
        response["graph"] = results_map["graph"]

    return response


@app.post("/api/evaluate")
async def evaluate(req: EvaluationRequest):
    import json
    eval_path = Path(__file__).parent.parent / "data" / "evaluations" / "user_evaluations.json"
    eval_path.parent.mkdir(parents=True, exist_ok=True)
    async with _eval_lock:
        evaluations = []
        if eval_path.exists():
            evaluations = json.loads(eval_path.read_text())
        evaluations.append(req.model_dump())
        eval_path.write_text(json.dumps(evaluations, ensure_ascii=False, indent=2))
    return {"status": "saved", "total_evaluations": len(evaluations)}


@app.get("/api/stats")
async def get_stats():
    import json
    eval_path = Path(__file__).parent.parent / "data" / "evaluations" / "user_evaluations.json"
    if not eval_path.exists():
        return {"total": 0, "legacy": {}, "improved": {}, "reranked": {}, "graph": {}}
    evaluations = json.loads(eval_path.read_text())

    def calc_stats(evals):
        return {
            "total": len(evals),
            "similar": sum(1 for e in evals if e["is_similar"]),
            "precision": round(sum(1 for e in evals if e["is_similar"]) / max(len(evals), 1), 4),
        }

    return {
        "total": len(evaluations),
        "legacy": calc_stats([e for e in evaluations if e["search_type"] == "legacy"]),
        "improved": calc_stats([e for e in evaluations if e["search_type"] == "improved"]),
        "reranked": calc_stats([e for e in evaluations if e["search_type"] == "reranked"]),
        "graph": calc_stats([e for e in evaluations if e["search_type"] == "graph"]),
    }
