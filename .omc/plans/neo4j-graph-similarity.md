# Neo4j Graph DB 기반 유사도 비교 시스템 추가 (v2 - Revised)

## Context

### Original Request
수학 문제 유사도 비교 시스템에 Neo4j Graph DB를 활용한 4번째 검색 방식을 추가한다. 기존 3가지 방식(Legacy 단일벡터, Improved 가중치분리벡터, LLM Reranking)과 나란히 비교 가능하게 한다.

### Current Architecture
- FastAPI + SPA 프론트엔드 (KaTeX 수학 렌더링), FastAPI는 Docker 밖에서 실행
- SQLite에 ~10만개 수학문제 + 3072d OpenAI 임베딩 저장 (full_text_vector, question_vector, solution_vector)
- 외부 MySQL(OCI `problem_bank`)에서 데이터 추출 → SQLite 저장
- 모든 벡터를 메모리에 로드 → numpy 코사인 유사도 계산
- 3-column 비교 UI (legacy / improved / reranked)
- SQLite `problems` 테이블에는 tag_ids(JSON 텍스트), main_category_tag_id 저장됨
- MySQL `problem` 테이블에도 `problem_vector(3072)` 컬럼 존재

### Key Insight: Tag Hierarchy is the KILLER Feature
MySQL의 `tag` 테이블은 `parent_id`로 5단계 계층 구조(depth1~depth5)를 형성한다. 이것이 그래프 DB의 핵심 가치:
- 같은 depth5 태그 = 거의 동일한 풀이법
- 같은 depth3, 다른 depth5 = 같은 개념, 다른 기법
- 그래프 탐색으로 태그 트리를 따라 관련 문제 발견 가능
- 벡터 유사도만으로는 이 구조적 관계를 명시적으로 포착 불가

### Interview Summary
- 사용자: thhwang (eduspace 파이프라인 개발자)
- 기존 비교 UI에 4번째 컬럼 추가
- Neo4j Community Edition (무료) 사용
- Docker Compose로 Neo4j 배포, FastAPI는 Docker 밖에서 실행

### Revision Notes (v2)
Critic 피드백 6개 Critical Issue + Minor Issue 모두 반영:
1. Tag 스키마 수정: `depth` 컬럼 없음, `parent_id` + `category` 필드 사용
2. `is_main` 분리: `problem_tag_bind`에는 `is_main` 없음, `problem.main_category_tag_id`는 별도 관계
3. 마이그레이션 데이터 소스 명확화: SQLite 문제(임베딩 보유) 기준 + MySQL에서 태그/관계 데이터 보강
4. Neo4j 메모리 설정 추가: 100K * 3072 * 4 bytes = ~1.2GB → JVM heap 설정
5. 실제 Cypher 쿼리 제공: 단일 배치 쿼리로 하이브리드 스코어링
6. Free-text 쿼리 폴백 전략: 그래프 노드 없는 경우 벡터 전용 + 태그 기반 보정
7. (Minor) `SIMILAR_GRADE` 엣지 제거, `search_by_graph_traversal` 역할 명확화
8. (Minor) 4-column 반응형 레이아웃, app.js 타입 맵 'graph' 추가

---

## Work Objectives

### Core Objective
Neo4j Graph DB를 4번째 검색 방식으로 추가하여, 태그 계층 기반 구조적 유사성과 벡터 유사도를 결합한 하이브리드 검색을 기존 방식들과 비교할 수 있게 한다.

### Deliverables
1. Neo4j 그래프 데이터 모델 (노드/엣지 정의) - 실제 MySQL 스키마 기반
2. MySQL + SQLite → Neo4j 데이터 마이그레이션 스크립트
3. Neo4j 기반 유사도 검색 서비스 (실제 Cypher 쿼리 포함)
4. `/api/search/graph` API 엔드포인트
5. `/api/search/compare` 확장 (4번째 결과 포함)
6. 웹 UI에 4번째 "Graph" 컬럼 추가
7. Docker Compose에 Neo4j 서비스 추가 (메모리 설정 포함)

### Definition of Done
- [ ] Neo4j Docker 컨테이너가 정상 기동되고 데이터가 로드됨
- [ ] `/api/search/graph` 엔드포인트가 유사 문제를 반환함
- [ ] `/api/search/compare`에 graph 결과가 포함됨
- [ ] 웹 UI에서 4개 컬럼으로 비교 가능
- [ ] 기존 3가지 검색 방식이 깨지지 않음
- [ ] Free-text 쿼리(문제 ID 없이 텍스트 직접 입력)가 정상 동작

---

## Must Have / Must NOT Have

### Must Have
- Neo4j Community Edition (무료) 사용
- Docker Compose로 Neo4j 설치/실행 (JVM heap 메모리 설정 포함)
- 기존 검색 방식과 동일한 결과 포맷 (score, search_type 포함)
- Graph 검색 on/off 토글
- 벡터 유사도 + 태그 계층 구조 유사도 결합 점수
- 단일 배치 Cypher 쿼리 (N+1 쿼리 방지)
- Free-text 쿼리 폴백 (벡터 전용 + 후보 태그 기반 보정)

### Must NOT Have
- Neo4j Enterprise Edition 유료 기능 사용하지 않음
- 기존 SQLite/벡터 검색 로직 변경하지 않음
- 기존 MySQL 추출 스크립트(`extract_problems.py`) 변경하지 않음
- 프론트엔드 전면 리디자인 하지 않음 (기존 패턴 따라 컬럼 추가만)
- `SIMILAR_GRADE` 같은 Problem→Problem 직접 엣지 만들지 않음 (grade는 속성 필터로 처리)

---

## Graph Data Model Design (REVISED - 실제 MySQL 스키마 기반)

### Nodes

| Label | Properties | Source |
|-------|-----------|--------|
| `Problem` | id(int), question(text), answer(text), grade(int), school_level(enum), type(enum), level(int), source_type(enum), main_category_tag_id(int), embedding(vector 3072d) | MySQL `problem` + SQLite 벡터 |
| `ProblemGroup` | id(int), instruction(text) | MySQL `problem_group` |
| `Source` | id(int), name(text), type(enum), exam_type(enum), year(int), grade(int), school_level(enum), math_subject(text) | MySQL `source_info` |
| `School` | id(int), kor_name(text), level(enum), city(text), district(text) | MySQL `school` |
| `Tag` | id(int), name(text), category(text), parent_id(int, nullable) | MySQL `tag` |

**Tag.category 값 참고:** 'depth1', 'depth2', 'depth3', 'depth4', 'depth5', 'depth6', '학년', '난이도', '자료유형', '교육과정', '대분류', '소분류', '출판사' 등

### Relationships (Edges)

| Type | From | To | Properties | Notes |
|------|------|----|-----------|-------|
| `BELONGS_TO_GROUP` | Problem | ProblemGroup | - | problem.group_id → problem_group.id |
| `HAS_SOURCE` | ProblemGroup | Source | - | problem_group.source_id → source_info.id |
| `FROM_SCHOOL` | Source | School | - | source_info.school_id → school.id |
| `HAS_TAG` | Problem | Tag | - | problem_tag_bind (problem_id, tag_id) |
| `MAIN_TAG` | Problem | Tag | - | problem.main_category_tag_id → tag.id (별도 관계) |
| `CHILD_OF` | Tag | Tag | - | tag.parent_id → tag.id (태그 계층 구조) |

**주의: `HAS_TAG`와 `MAIN_TAG`는 다른 관계**
- `HAS_TAG`: `problem_tag_bind` 테이블 기반 (평균 9.6개/문제, 총 1,550만행)
- `MAIN_TAG`: `problem.main_category_tag_id` 컬럼 기반 (문제당 0~1개)
- 두 관계를 혼동하면 안 됨

### Vector Index
```cypher
CREATE VECTOR INDEX problem_embedding FOR (p:Problem) ON (p.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 3072,
  `vector.similarity_function`: 'cosine'
}}
```

### Tag Hierarchy Index (성능 최적화)
```cypher
CREATE INDEX tag_category FOR (t:Tag) ON (t.category);
CREATE INDEX tag_parent FOR (t:Tag) ON (t.parent_id);
CREATE INDEX problem_grade FOR (p:Problem) ON (p.grade);
CREATE INDEX problem_school_level FOR (p:Problem) ON (p.school_level);
```

---

## Hybrid Scoring Strategy (REVISED)

### 점수 계산 공식
```
final_score = alpha * vector_similarity + beta * tag_similarity

where:
  vector_similarity = Neo4j vector index cosine similarity (0.0~1.0)

  tag_similarity = weighted Jaccard with depth bonus:
    - depth5 공유: 가중치 5 (거의 동일한 풀이법)
    - depth4 공유: 가중치 4
    - depth3 공유: 가중치 3 (같은 개념)
    - depth2 공유: 가중치 2
    - depth1 공유: 가중치 1
    - 기타 태그(학년, 난이도 등) 공유: 가중치 0.5

  tag_similarity = sum(shared_tag_weights) / sum(all_query_tag_weights)
  (0.0~1.0 정규화)

Default weights: alpha=0.6, beta=0.4
```

### Two Search Modes

**Mode A: Problem-ID 기반 검색 (기존 문제에서 유사 문제 찾기)**
- 쿼리 문제의 그래프 노드가 존재함
- 벡터 유사도 + 태그 공유 기반 하이브리드 점수 계산
- 단일 배치 Cypher 쿼리로 처리

**Mode B: Free-text 기반 검색 (직접 텍스트 입력)**
- 그래프 노드가 없음 → `graph_similarity` 직접 계산 불가
- 폴백 전략:
  1. 벡터 검색으로 top_k * 3 후보 확보
  2. 후보들 간 태그 클러스터링: 가장 많이 등장하는 depth3~5 태그를 "추정 태그"로 선정
  3. 추정 태그 기반으로 graph_similarity 보정 점수 계산
  4. `alpha * vec_score + beta * inferred_tag_score`로 최종 정렬
  5. 이 모드에서는 `graph_score`에 "(추정)" 표시

---

## Core Cypher Queries (REVISED - 실제 배치 쿼리)

### Query 1: Problem-ID 기반 하이브리드 검색 (Mode A)
```cypher
// Step 1: 쿼리 문제의 임베딩과 태그 수집
MATCH (query:Problem {id: $query_id})
WITH query, query.embedding AS qvec

// Step 2: 쿼리 문제의 태그와 깊이 가중치 수집
OPTIONAL MATCH (query)-[:HAS_TAG]->(qt:Tag)
WITH query, qvec,
     collect({id: qt.id, category: qt.category}) AS query_tags

// Step 3: 벡터 검색으로 후보군 확보
CALL db.index.vector.queryNodes('problem_embedding', $candidate_k, qvec)
YIELD node AS candidate, score AS vec_score
WHERE candidate.id <> $query_id
  AND ($grade IS NULL OR candidate.grade = $grade)
  AND ($school_level IS NULL OR candidate.school_level = $school_level)

// Step 4: 각 후보의 태그 수집 + 공유 태그 계산 (배치)
WITH candidate, vec_score, query_tags
OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)
WITH candidate, vec_score, query_tags,
     collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

// Step 5: 태그 유사도 계산 (depth 가중치 적용)
WITH candidate, vec_score, query_tags, cand_tags,
     [qt IN query_tags WHERE qt.id IN [ct IN cand_tags | ct.id]] AS shared,
     // depth 가중치 맵핑
     CASE
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

// Step 6: 하이브리드 점수 계산 + 정렬
WITH candidate, vec_score, tag_score,
     ($alpha * vec_score + $beta * tag_score) AS final_score,
     [ct IN cand_tags WHERE ct.id IN [qt IN query_tags | qt.id]] AS shared_tags_info
ORDER BY final_score DESC
LIMIT $top_k

RETURN candidate.id AS id,
       candidate.question AS question,
       candidate.answer AS answer,
       candidate.grade AS grade,
       candidate.school_level AS school_level,
       candidate.type AS type,
       candidate.level AS level,
       round(final_score, 4) AS score,
       round(vec_score, 4) AS vector_score,
       round(tag_score, 4) AS graph_score,
       [st IN shared_tags_info | st.name] AS shared_tags
```

### Query 2: Free-text 벡터 전용 + 태그 추정 (Mode B)
```cypher
// Step 1: 임베딩으로 후보군 확보
CALL db.index.vector.queryNodes('problem_embedding', $candidate_k, $query_embedding)
YIELD node AS candidate, score AS vec_score
WHERE ($grade IS NULL OR candidate.grade = $grade)
  AND ($school_level IS NULL OR candidate.school_level = $school_level)

// Step 2: 후보들의 태그 수집
WITH candidate, vec_score
OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)
WITH candidate, vec_score,
     collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

// Step 3: 결과 반환 (태그 정보 포함, graph_score는 Python에서 후처리)
ORDER BY vec_score DESC
LIMIT $candidate_k

RETURN candidate.id AS id,
       candidate.question AS question,
       candidate.answer AS answer,
       candidate.grade AS grade,
       candidate.school_level AS school_level,
       candidate.type AS type,
       candidate.level AS level,
       round(vec_score, 4) AS vector_score,
       cand_tags AS tags
```

**Python 후처리 (Mode B):**
```python
# 1. 상위 후보들에서 가장 빈번한 depth3~5 태그 추출 → 추정 쿼리 태그
top_candidates = results[:10]  # 상위 10개에서 태그 빈도 분석
tag_freq = Counter()
for c in top_candidates:
    for t in c['tags']:
        if t['category'] in ('depth3', 'depth4', 'depth5'):
            tag_freq[t['id']] += 1
inferred_tags = [tid for tid, cnt in tag_freq.most_common(5) if cnt >= 2]

# 2. 각 후보에 대해 추정 태그 기반 tag_score 계산
for c in all_candidates:
    cand_tag_ids = {t['id'] for t in c['tags']}
    shared = len(cand_tag_ids & set(inferred_tags))
    c['graph_score'] = shared / max(len(inferred_tags), 1)
    c['score'] = alpha * c['vector_score'] + beta * c['graph_score']
    c['graph_score_inferred'] = True  # UI에서 "(추정)" 표시용

# 3. 최종 정렬
results = sorted(all_candidates, key=lambda x: x['score'], reverse=True)[:top_k]
```

### Query 3: 태그 계층 탐색 (보조 기능 - graph traversal용)
```cypher
// 특정 문제의 태그에서 시작하여 같은 태그 계층의 문제 탐색
MATCH (query:Problem {id: $problem_id})-[:HAS_TAG]->(qt:Tag)
WHERE qt.category IN ['depth3', 'depth4', 'depth5']

// 같은 태그를 가진 다른 문제 찾기
MATCH (other:Problem)-[:HAS_TAG]->(qt)
WHERE other.id <> $problem_id
  AND ($grade IS NULL OR other.grade = $grade)

// 공유 태그 수와 깊이로 점수 계산
WITH other,
     collect(DISTINCT qt.name) AS shared_tags,
     count(DISTINCT qt) AS shared_count,
     sum(CASE qt.category
       WHEN 'depth5' THEN 5 WHEN 'depth4' THEN 4
       WHEN 'depth3' THEN 3 ELSE 1 END) AS weighted_count
ORDER BY weighted_count DESC
LIMIT $top_k

RETURN other.id AS id,
       other.question AS question,
       other.grade AS grade,
       other.school_level AS school_level,
       shared_tags,
       shared_count,
       weighted_count
```

**`search_by_graph_traversal`의 역할 명확화:**
- 이 함수는 `/api/search/graph`의 보조 기능으로, 벡터 없이 순수 그래프 구조만으로 검색
- `/api/search/graph` 엔드포인트에서 `mode=traversal` 파라미터로 선택 가능
- 주요 용도: "이 문제와 같은 단원/개념의 문제 모두 보기" (그래프 탐색 데모)
- compare API에는 포함하지 않음 (하이브리드 모드만 포함)

---

## Migration Strategy (REVISED - 데이터 소스 명확화)

### 데이터 소스 및 조인 전략

```
Migration Data Sources:
┌─────────────────────────────────────────────────────────────┐
│ SQLite (math_problems.db)                                    │
│ - ~100K problems with embeddings (full_text_vector 3072d)   │
│ - 각 문제의 id가 MySQL problem.id와 동일                      │
│ - tag_ids (JSON text), main_category_tag_id 저장됨           │
│ → PRIMARY SOURCE: Problem 노드 + 임베딩                      │
├─────────────────────────────────────────────────────────────┤
│ MySQL (problem_bank)                                         │
│ - problem_tag_bind: problem_id ↔ tag_id 매핑 (15.5M rows)   │
│ - tag: 계층 구조 (26K tags, parent_id 기반)                   │
│ - problem_group, source_info, school: 관계형 메타데이터       │
│ → SUPPLEMENTARY: 태그 관계 + 메타데이터                       │
└─────────────────────────────────────────────────────────────┘

Join Strategy:
1. SQLite에서 모든 problem id 목록 추출
2. MySQL에서 해당 id들의 problem_tag_bind 조회 (WHERE problem_id IN (...))
3. MySQL에서 관련 tag, problem_group, source_info, school 조회
4. SQLite 임베딩 + MySQL 관계 데이터를 합쳐서 Neo4j에 적재
```

### 마이그레이션 단계

**Phase 1: 데이터 수집**
```python
# 1. SQLite에서 문제 ID + 임베딩 로드
sqlite_problems = load_sqlite_problems()  # id, embedding, grade, school_level, etc.
problem_ids = [p['id'] for p in sqlite_problems]

# 2. MySQL에서 태그 바인딩 조회 (배치)
# WHERE problem_id IN (...) 로 SQLite에 있는 문제만
tag_bindings = fetch_tag_bindings(problem_ids)  # [(problem_id, tag_id), ...]

# 3. MySQL에서 태그 계층 전체 로드
all_tags = fetch_all_math_tags()  # id, parent_id, name, category

# 4. MySQL에서 관계 메타데이터 (이미 SQLite에 일부 있으므로 group/source/school만 보강)
groups = fetch_problem_groups(problem_ids)
sources = fetch_source_infos(...)
schools = fetch_schools(...)
```

**Phase 2: Neo4j 적재 (UNWIND 배치)**
```python
# 배치 크기: 500개씩
BATCH_SIZE = 500

# 1. Tag 노드 생성 (26K개, 한번에 가능)
driver.execute_query("""
  UNWIND $tags AS t
  MERGE (tag:Tag {id: t.id})
  SET tag.name = t.name, tag.category = t.category, tag.parent_id = t.parent_id
""", tags=all_tags)

# 2. Tag CHILD_OF 관계 생성
driver.execute_query("""
  MATCH (child:Tag) WHERE child.parent_id IS NOT NULL
  MATCH (parent:Tag {id: child.parent_id})
  MERGE (child)-[:CHILD_OF]->(parent)
""")

# 3. Problem 노드 생성 (배치 500개씩, 임베딩 포함)
for batch in chunks(sqlite_problems, BATCH_SIZE):
    driver.execute_query("""
      UNWIND $problems AS p
      MERGE (prob:Problem {id: p.id})
      SET prob.question = p.question, prob.grade = p.grade,
          prob.school_level = p.school_level, prob.type = p.type,
          prob.level = p.level, prob.main_category_tag_id = p.main_category_tag_id,
          prob.embedding = p.embedding
    """, problems=batch)

# 4. HAS_TAG 관계 생성 (배치)
for batch in chunks(tag_bindings, BATCH_SIZE):
    driver.execute_query("""
      UNWIND $bindings AS b
      MATCH (p:Problem {id: b.problem_id})
      MATCH (t:Tag {id: b.tag_id})
      MERGE (p)-[:HAS_TAG]->(t)
    """, bindings=batch)

# 5. MAIN_TAG 관계 생성 (별도)
driver.execute_query("""
  MATCH (p:Problem) WHERE p.main_category_tag_id IS NOT NULL
  MATCH (t:Tag {id: p.main_category_tag_id})
  MERGE (p)-[:MAIN_TAG]->(t)
""")

# 6. ProblemGroup, Source, School 노드 + 관계 (배치)
# ... (기존 패턴과 동일)

# 7. Vector Index 생성
driver.execute_query("""
  CREATE VECTOR INDEX problem_embedding IF NOT EXISTS
  FOR (p:Problem) ON (p.embedding)
  OPTIONS {indexConfig: {`vector.dimensions`: 3072, `vector.similarity_function`: 'cosine'}}
""")

# 8. 보조 인덱스 생성
driver.execute_query("CREATE INDEX tag_category IF NOT EXISTS FOR (t:Tag) ON (t.category)")
# ... etc.
```

### 예상 소요 시간
- Tag 노드: 26K → 수초
- Problem 노드 (임베딩 포함): 100K → 5~10분
- HAS_TAG 관계: 최대 960K (100K * 9.6 평균, SQLite 문제만) → 3~5분
- CHILD_OF 관계: 26K → 수초
- Vector Index 빌드: 100K * 3072d → 5~10분
- **총: 15~30분**

---

## Task Flow and Dependencies

```
Task 1 (Docker/Infra + 메모리 설정) ──────┐
                                           │
Task 2 (Migration Script) ──── depends on ─┤
                                           │
Task 3 (Neo4j Service + Cypher) ── depends ┤
                                           │
Task 4 (API Endpoints) ──── depends on Task 3
                                           │
Task 5 (Frontend UI) ──── depends on Task 4
                                           │
Task 6 (Compare API + 통합) ── depends on Task 3, Task 4
```

---

## Detailed TODOs

### Task 1: Docker 인프라 설정 (메모리 설정 포함)
**Files:** `docker-compose.yml` (신규), `.env.example` (수정), `requirements.txt` (수정)
**Acceptance Criteria:** `docker-compose up -d neo4j`로 Neo4j 기동, `localhost:7474` 접속 가능, 메모리 설정 적용 확인

1. `docker-compose.yml` 생성
   ```yaml
   version: '3.8'
   services:
     neo4j:
       image: neo4j:5.26-community  # Vector Index 지원 5.13+
       container_name: math-similarity-neo4j
       ports:
         - "7474:7474"  # Browser
         - "7687:7687"  # Bolt
       volumes:
         - ./data/neo4j/data:/data
         - ./data/neo4j/logs:/logs
         - ./data/neo4j/plugins:/plugins
       environment:
         - NEO4J_AUTH=${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD:-changeme}
         # JVM Heap 설정 (100K * 3072 * 4B = ~1.2GB for vectors)
         - NEO4J_server_memory_heap_initial__size=2g
         - NEO4J_server_memory_heap_max__size=3g
         - NEO4J_server_memory_pagecache_size=1g
         # Vector Index 활성화
         - NEO4J_dbms_security_procedures_unrestricted=apoc.*
         # APOC 플러그인
         - NEO4J_PLUGINS=["apoc"]
       restart: unless-stopped
       healthcheck:
         test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:7474 || exit 1"]
         interval: 15s
         timeout: 10s
         retries: 5
   ```

   **메모리 계산 근거:**
   - Vector 데이터: 100K problems * 3072 dimensions * 4 bytes = ~1.2GB
   - Tag/관계 데이터: ~100MB
   - Vector Index: ~1.2GB (원본과 비슷)
   - 총 필요: ~2.5GB → heap 3GB + pagecache 1GB = 4GB 권장
   - 호스트 머신 최소 RAM: 6GB (OS + FastAPI + Neo4j)

2. `.env.example`에 추가
   ```
   # Neo4j
   NEO4J_URI=bolt://localhost:7687
   NEO4J_USER=neo4j
   NEO4J_PASSWORD=your_neo4j_password
   ```

3. `requirements.txt`에 `neo4j>=5.0.0` 추가

### Task 2: 데이터 마이그레이션 스크립트
**Files:** `scripts/migrate_to_neo4j.py` (신규)
**Acceptance Criteria:** 스크립트 실행 후 Neo4j에 Problem, Tag, CHILD_OF, HAS_TAG, MAIN_TAG 등 모든 노드/관계 생성 확인

1. **SQLite에서 문제 데이터 + 임베딩 로드**
   - `problems` 테이블에서 id, question, grade, school_level 등 메타 + full_text_vector BLOB
   - BLOB → `list[float]` 변환 (`np.frombuffer(blob, dtype=np.float32).tolist()`)

2. **MySQL에서 태그 및 관계 데이터 추출**
   - `tag` 테이블 전체 (id, parent_id, name, category) → 수학 관련만 필터 (parent_id 1835, 1836 하위 + 학년/난이도/자료유형 등)
   - `problem_tag_bind` 테이블에서 SQLite problem_id에 해당하는 바인딩만 추출
   - `problem_group`, `source_info`, `school` 테이블 (기존 extract_problems.py와 동일한 조인)

3. **Neo4j 적재** (위 Migration Strategy 섹션의 코드 참조)
   - Tag 노드 → CHILD_OF 관계 → Problem 노드(임베딩) → HAS_TAG → MAIN_TAG → Group/Source/School → Vector Index → 보조 인덱스
   - 배치 크기 500, tqdm 진행 표시
   - 각 단계 완료 후 카운트 출력

4. **검증 쿼리 실행**
   ```cypher
   MATCH (p:Problem) RETURN count(p) AS problems;
   MATCH (t:Tag) RETURN count(t) AS tags;
   MATCH ()-[r:HAS_TAG]->() RETURN count(r) AS tag_bindings;
   MATCH ()-[r:CHILD_OF]->() RETURN count(r) AS hierarchy_edges;
   ```

### Task 3: Neo4j 검색 서비스 구현
**Files:** `app/db/neo4j_client.py` (신규), `app/services/graph_search_service.py` (신규)
**Acceptance Criteria:** `GraphSearchService.search_hybrid()` 호출 시 하이브리드 점수로 정렬된 유사 문제 리스트 반환. free-text 모드에서도 정상 동작.

1. `app/db/neo4j_client.py` - Neo4j 드라이버 래퍼
   - `neo4j.AsyncDriver` 사용
   - 환경변수 `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`에서 연결 정보 로드
   - `connect()`, `close()`, `execute_query()` 메서드
   - 앱 startup/shutdown 이벤트에서 연결 관리

2. `app/services/graph_search_service.py`

   - `search_hybrid(problem_id: int | None, query_embedding: list[float], top_k, filters, alpha, beta)`
     - **Mode A** (problem_id 있을 때): Query 1 Cypher 실행 (단일 배치 쿼리)
     - **Mode B** (problem_id 없을 때, free-text):
       1. Query 2 Cypher로 벡터 후보 확보 (top_k * 3)
       2. Python에서 태그 빈도 분석 → 추정 태그 선정
       3. 추정 태그 기반 graph_score 계산
       4. 하이브리드 점수로 재정렬
       5. 결과에 `graph_score_inferred: true` 표시

   - `search_by_graph_traversal(problem_id: int, top_k, filters)`
     - Query 3 Cypher 실행 (순수 그래프 탐색)
     - 벡터 유사도 없이 태그 계층 구조만으로 검색
     - `/api/search/graph?mode=traversal`로 호출 가능 (선택적 기능)

   - 결과 포맷:
     ```python
     {
       "id": int,
       "question": str,
       "grade": int,
       "school_level": str,
       "score": float,           # 최종 하이브리드 점수
       "vector_score": float,    # 벡터 유사도
       "graph_score": float,     # 태그 구조 유사도
       "shared_tags": list[str], # 공유 태그 이름 목록
       "graph_score_inferred": bool,  # True면 free-text 추정 모드
       "search_type": "graph"
     }
     ```

   - **추가 데이터 보강**: 결과 Problem ID로 SQLite에서 question_text, solution_text, refer, choice1~5 등 상세 데이터 조회 (Neo4j에는 최소 데이터만 저장)

### Task 4: API 엔드포인트 추가
**Files:** `app/main.py` (수정), `app/config.py` (수정)
**Acceptance Criteria:** `/api/search/graph` POST 엔드포인트 작동, `/api/search/compare`에 graph 결과 포함

1. `app/config.py`에 Neo4j 환경변수 추가
   ```python
   NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
   NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
   NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
   ```

2. `SearchRequest` 모델 확장
   ```python
   graph_enabled: bool = False
   graph_alpha: float = 0.6
   graph_beta: float = 0.4
   ```

3. `EvaluationRequest.search_type` 주석 업데이트: `"legacy"`, `"improved"`, `"reranked"`, `"graph"`

4. `/api/search/graph` POST 엔드포인트
   - `state.queryProblemId`가 있으면 Mode A (problem_id 기반)
   - 없으면 Mode B (free-text, 임베딩 생성 후 검색)
   - 기존 `EmbeddingService`로 쿼리 임베딩 생성

5. `/api/search/compare` 확장
   - `graph_enabled=True`일 때 graph 검색도 `asyncio.gather`에 포함
   - 응답에 `"graph": [...]` 키 추가

6. `/api/stats` 확장
   - graph 타입 평가 통계 포함

7. startup/shutdown 이벤트에 Neo4j driver 연결/해제 추가

### Task 5: 프론트엔드 UI 수정
**Files:** `app/static/index.html` (수정), `app/static/app.js` (수정), `app/static/styles.css` (수정)
**Acceptance Criteria:** Graph 검색 활성화 시 4번째 컬럼이 표시되고, 결과 카드에 graph_score와 shared_tags가 표시됨

1. `index.html` 수정
   - Input panel에 "Graph Search" 체크박스 추가 (LLM Reranking 옆)
     - alpha/beta 슬라이더 (graph_enabled 체크 시 표시)
   - Results grid에 4번째 컬럼 `col-graph` 추가
     - 헤더: "Graph DB", 서브타이틀: "Neo4j 벡터 + 태그 구조"
     - 색상: orange 계열
   - Stats footer에 Graph Precision 추가
   - Header stats bar에 Graph Precision pill 추가

2. `app.js` 수정
   - `state.currentResults`에 `graph: []` 추가
   - `buildSearchRequest()`에 `graph_enabled`, `graph_alpha`, `graph_beta` 추가
   - `searchCompare()`에서 graph 결과 처리
   - `setGraphColumnVisible(visible)` 함수 추가 (setRerankedColumnVisible 패턴)
   - `renderResults(legacy, improved, reranked, graph)` 시그니처 확장
   - Graph 결과 카드에 추가 정보 표시:
     - `graph_score` (태그 구조 점수)
     - `vector_score` (벡터 유사도)
     - `shared_tags` (공유 태그 배지)
     - `graph_score_inferred` true이면 "(추정)" 표시
   - Results grid CSS class: `four-columns` 추가
   - **타입 맵 업데이트**: 평가 기능에서 `'graph'` 타입 지원
     ```javascript
     const typeLabelMap = { legacy: '기존', improved: '신규', reranked: 'LLM Reranking', graph: 'Graph DB' };
     const typeColorMap = { ..., graph: { bg: 'var(--orange-50)', color: 'var(--orange-700)', border: 'var(--orange-100)' } };
     ```
   - `evaluate()` 함수의 typeLabel에 graph 추가

3. `styles.css` 수정
   - **반응형 4-column 레이아웃**:
     ```css
     .four-columns {
       grid-template-columns: repeat(4, 1fr);
     }
     /* 화면 너비 좁을 때 2x2 그리드로 전환 */
     @media (max-width: 1400px) {
       .four-columns {
         grid-template-columns: repeat(2, 1fr);
       }
     }
     @media (max-width: 768px) {
       .four-columns {
         grid-template-columns: 1fr;
       }
     }
     ```
   - Orange 색상 변수 추가
     ```css
     --orange-50: #fff7ed;
     --orange-100: #ffedd5;
     --orange-600: #ea580c;
     --orange-700: #c2410c;
     ```
   - `.col-dot-graph`, `.column-header-graph` 스타일
   - `.shared-tag-badge` 스타일 (공유 태그 표시용)
   - `.inferred-badge` 스타일 ("추정" 표시용)

### Task 6: Compare API 통합 및 테스트
**Files:** `app/main.py` (Task 4에서 이어서)
**Acceptance Criteria:** 4가지 방식 동시 비교가 정상 작동

1. `/api/search/compare`에서 graph 포함 시 4-way 병렬 실행 (`asyncio.gather`)
2. `/api/stats` 확장 - graph 타입 평가 통계 포함
3. 수동 테스트 시나리오:
   - [x] Mode A: 랜덤 문제 로드 → 4-way 비교 검색 → shared_tags 표시 확인
   - [x] Mode B: 직접 텍스트 입력 → graph 결과에 "(추정)" 표시 확인
   - [x] Graph on/off 토글 동작 확인
   - [x] 4-column 레이아웃 반응형 확인 (1400px 이하에서 2x2)
   - [x] 평가 기능 graph 타입 정상 저장 확인

---

## Commit Strategy

| Commit | Content |
|--------|---------|
| 1 | Docker Compose + .env.example + requirements.txt (Neo4j 인프라) |
| 2 | `scripts/migrate_to_neo4j.py` (마이그레이션 스크립트) |
| 3 | `app/db/neo4j_client.py` + `app/services/graph_search_service.py` (서비스 레이어) |
| 4 | `app/main.py`, `app/config.py` (API endpoints) |
| 5 | `app/static/*` (Frontend UI 4번째 컬럼) |
| 6 | 통합 테스트 + 마무리 |

---

## Success Criteria

1. **기능 완성**: Neo4j 하이브리드 검색이 작동하고, 기존 방식과 나란히 비교 가능
2. **비파괴**: 기존 3가지 검색 방식 모두 정상 (regression 없음)
3. **UX 일관성**: 4번째 컬럼이 기존 UI 패턴과 자연스럽게 어울림 (반응형 포함)
4. **Graph 차별점 가시화**: shared_tags, graph_score, 태그 depth 가중치가 결과에 명확히 표시
5. **설치 용이**: Docker Compose 한 줄로 Neo4j 환경 구축 (메모리 설정 포함)
6. **Free-text 지원**: 문제 ID 없이 텍스트만 입력해도 graph 검색 동작 (추정 모드)
7. **성능**: 단일 배치 Cypher 쿼리로 N+1 문제 없음

---

## Notes

- Neo4j Community Edition은 무료이며 Vector Index를 5.13+ 버전에서 지원
- 100K 문제의 3072d 벡터: ~1.2GB 메모리 (JVM heap 3GB + pagecache 1GB 설정 필요)
- 호스트 머신 최소 6GB RAM 권장 (OS + FastAPI + Neo4j)
- 초기 마이그레이션 15~30분 소요 (HAS_TAG 관계가 가장 오래 걸림)
- `problem_tag_bind`는 전체 15.5M행이지만, SQLite에 있는 ~100K 문제만 마이그레이션하면 ~960K행
- Graph 검색은 벡터 검색 대비 다소 느릴 수 있음 → UI에 소요시간 표시 고려
- `SIMILAR_GRADE` 엣지는 사용하지 않음 (grade는 Problem 노드 속성 필터로 처리)
- FastAPI는 Docker 밖에서 실행, Neo4j만 Docker로 실행 (bolt://localhost:7687 연결)
