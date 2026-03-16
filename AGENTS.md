# Math Problem Similarity — AGENTS.md

## 프로젝트 개요

수학 문제 유사도 검색 A/B 비교 시스템. 4가지 검색 방식을 나란히 비교하여 최적의 유사 문제 검색 방법을 평가한다.

- **서버**: FastAPI + Uvicorn (포트 9222)
- **프론트엔드**: SPA (KaTeX 수학 렌더링)
- **데이터**: SQLite 100K 수학 문제 + 3072d OpenAI 임베딩

## 실행 방법

```bash
# 1. Neo4j 시작 (Graph DB 검색용)
docker compose up -d neo4j

# 2. FastAPI 서버 시작
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 9222

# 3. Neo4j 데이터 마이그레이션 (최초 1회)
python scripts/migrate_to_neo4j.py --clear
```

## 아키텍처

```
브라우저 (localhost:9222)
  ↓ HTTP
FastAPI (app/main.py)
  ├── SearchService (벡터 검색) → SQLite (data/math_problems.db)
  ├── RerankingService (LLM 재정렬) → GPT-OSS / Gemini
  ├── GraphSearchService (그래프 검색) → Neo4j (localhost:7687)
  └── EmbeddingService (임베딩 생성) → OpenAI API
```

## 4가지 검색 방식

| # | 방식 | 설명 | 코드 |
|---|------|------|------|
| 1 | **Legacy** | 문제+해설 결합 텍스트의 단일 벡터 코사인 유사도 | `search_service.py:search_legacy()` |
| 2 | **Improved** | 문제/해설 분리 임베딩 + 가중치 결합 (기본 Q:0.3, S:0.7) | `search_service.py:search_improved()` |
| 3 | **LLM Reranking** | Improved 후보를 LLM이 0~10점으로 재평가 | `reranking_service.py:rerank()` |
| 4 | **Graph DB** | Neo4j 벡터 유사도 + 태그 계층 구조 유사도 하이브리드 | `graph_search_service.py:search_hybrid()` |

## 파일 구조

```
app/
├── main.py                    # FastAPI 엔드포인트 (6개 검색 + 평가 + 통계)
├── config.py                  # 환경변수 로드 (MySQL, OpenAI, Neo4j)
├── db/
│   ├── mysql_client.py        # OCI MySQL 연결 (문제 원본 조회)
│   └── neo4j_client.py        # Neo4j 드라이버 래퍼
├── services/
│   ├── search_service.py      # Legacy + Improved 벡터 검색
│   ├── embedding_service.py   # OpenAI text-embedding-3-large (3072d)
│   ├── reranking_service.py   # LLM 재정렬 (GPT-OSS / Gemini)
│   ├── graph_search_service.py # Neo4j 하이브리드 검색
│   └── tagging_service.py     # 2단계 LLM 태그 분류
├── static/
│   ├── index.html             # SPA 메인 페이지
│   ├── app.js                 # 프론트엔드 로직
│   └── styles.css             # 스타일시트
scripts/
├── extract_problems.py        # MySQL → SQLite 문제 추출
├── generate_embeddings.py     # 임베딩 생성 (OpenAI API)
├── migrate_to_neo4j.py        # SQLite+MySQL → Neo4j 마이그레이션
└── auto_tag_problems.py       # 자동 태그 분류
data/
├── math_problems.db           # SQLite DB (100K 문제 + 3종 임베딩, ~4GB)
└── neo4j/                     # Neo4j 데이터 (Docker volume)
```

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 메인 웹 페이지 |
| GET | `/api/problem/random` | 랜덤 문제 조회 |
| GET | `/api/problem/{id}` | ID로 문제 조회 (MySQL) |
| POST | `/api/search/legacy` | Legacy 벡터 검색 |
| POST | `/api/search/improved` | Improved 가중치 벡터 검색 |
| POST | `/api/search/rerank` | Improved + LLM Reranking |
| POST | `/api/search/graph` | Neo4j 하이브리드 검색 |
| POST | `/api/search/compare` | 최대 4가지 동시 비교 검색 |
| POST | `/api/evaluate` | 유사/비유사 평가 저장 |
| GET | `/api/stats` | 평가 통계 (Precision@K) |

## Graph DB 상세

### 그래프 모델
- **Problem** 노드: 100K 문제 + 3072d 벡터 임베딩
- **Tag** 노드: ~16,650개 활성 태그 (deactivated 제외)
- **CHILD_OF** 관계: 태그 계층 (depth1→depth5)
- **HAS_TAG** 관계: 문제-태그 바인딩 (평균 9.6개/문제)
- **ProblemGroup, Source, School** 노드: 메타데이터

### 하이브리드 스코어링
```
final_score = α(0.6) × vector_similarity + β(0.4) × tag_similarity

tag_similarity = depth 가중치 Jaccard:
  depth5 공유: 5 (동일 풀이법), depth4: 4, depth3: 3, depth2: 2, depth1: 1
```

### 두 가지 모드
- **Mode A** (Problem-ID): 기존 문제 노드 기반 → 단일 배치 Cypher 쿼리
- **Mode B** (Free-text): 벡터 후보에서 태그 빈도 분석 → 추정 태그 기반 보정

### Deactivated 태그 필터링
- `tag.desc = 'deactivated'`인 태그 + 하위 자식 재귀적 제외
- 마이그레이션 시 필터링, Neo4j에는 활성 태그만 적재

## 데이터 소스

| DB | 용도 | 접속 |
|----|------|------|
| SQLite (`data/math_problems.db`) | 문제 데이터 + 임베딩 (런타임 검색) | 로컬 파일 |
| MySQL (OCI `problem_bank`) | 원본 데이터 추출, 태그/관계 조회 | localhost:33108 |
| Neo4j (Docker) | 그래프 기반 유사도 검색 | localhost:7687 |

## 환경변수 (.env)

```
# MySQL
OCI_DB_PROD_HOST, OCI_DB_PROD_PORT, OCI_DB_PROD_USER, OCI_DB_PROD_PASSWORD, OCI_DB_PROD_NAME

# OpenAI
OPENAI_API_KEY_EMBEDDING

# Dev LLM
DEV_LLM_URL, DEV_LLM_KEY, DEV_LLM_NAME

# OpenRouter
OPENROUTER_API_KEY, OPENROUTER_MODEL

# Neo4j
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
```

## 프론트엔드 기능

- 4컬럼 비교 UI (Legacy/Improved/Reranked/Graph)
- **컬럼 토글**: 상단 토글 바에서 원하는 컬럼만 표시/숨김 (1~4열 자동 조절)
- LLM Reranking / Graph DB 검색 on/off 체크박스
- 가중치 슬라이더 (Q/S, α/β)
- KaTeX 수학 수식 렌더링
- 결과 카드 클릭 → 상세 모달
- 유사/비유사 평가 버튼 → Precision@K 통계
