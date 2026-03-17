# Neo4j Graph DB 가이드 — math-problem-similarity

이 문서는 Graph DB를 처음 사용하는 개발자를 대상으로, 이 프로젝트에서 Neo4j가 어떻게 구동되고 내부 로직이 어떻게 돌아가는지 단계별로 설명합니다.

---

## 목차

1. [Graph DB 기본 개념](#1-graph-db-기본-개념)
2. [이 프로젝트의 데이터 구조](#2-이-프로젝트의-데이터-구조)
3. [Docker로 Neo4j 구동](#3-docker로-neo4j-구동)
4. [데이터 마이그레이션 과정](#4-데이터-마이그레이션-과정)
5. [검색 로직 상세 설명](#5-검색-로직-상세-설명)
6. [API 전체 흐름](#6-api-전체-흐름)
7. [프론트엔드 UI 설명](#7-프론트엔드-ui-설명)
8. [실제 Cypher 쿼리 예시](#8-실제-cypher-쿼리-예시)
9. [성능 관련 팁](#9-성능-관련-팁)

---

## 1. Graph DB 기본 개념

### 관계형 DB와 무엇이 다른가

일반적인 관계형 DB(MySQL, SQLite)는 데이터를 테이블 형태로 저장하고, 테이블 간 관계는 외래키(FK)와 JOIN으로 표현합니다. 반면 Graph DB는 **데이터 자체를 그래프 구조**로 저장합니다.

```
관계형 DB:
  problems 테이블 ─── JOIN ─── problem_tag_bind 테이블 ─── JOIN ─── tag 테이블

Graph DB:
  (Problem) ──[HAS_TAG]──> (Tag) ──[CHILD_OF]──> (Tag)
```

Graph DB는 연결 자체가 1급 시민(first-class citizen)입니다. 관계를 탐색하는 데 JOIN 비용이 없고, 노드에서 노드로 따라가는 "그래프 순회(traversal)"가 매우 빠릅니다.

### 핵심 구성 요소

| 개념 | 설명 | 예시 |
|------|------|------|
| **노드 (Node)** | 데이터 엔티티. 레이블로 타입을 구분한다 | `(p:Problem)`, `(t:Tag)` |
| **관계 (Relationship)** | 노드 간의 방향 있는 연결. 타입이 있다 | `-[:HAS_TAG]->`, `-[:CHILD_OF]->` |
| **속성 (Property)** | 노드나 관계에 붙는 키-값 데이터 | `p.id`, `p.embedding`, `t.category` |
| **레이블 (Label)** | 노드의 타입 분류자 | `Problem`, `Tag`, `Source` |

### Cypher 쿼리 언어

Neo4j는 SQL 대신 **Cypher**라는 쿼리 언어를 사용합니다. 그래프의 패턴을 ASCII 아트처럼 표현하는 것이 특징입니다.

```cypher
-- SQL: SELECT * FROM problems JOIN problem_tag_bind ON ... JOIN tag ON ...
-- Cypher: 같은 의미
MATCH (p:Problem)-[:HAS_TAG]->(t:Tag)
RETURN p.id, t.name
```

주요 구문:

```cypher
-- 노드 찾기
MATCH (p:Problem {id: 12345})

-- 관계 탐색
MATCH (p:Problem)-[:HAS_TAG]->(t:Tag)

-- 필터링
WHERE t.category = 'depth3'

-- 집계
WITH p, collect(t.name) AS tags

-- 정렬 및 제한
ORDER BY score DESC
LIMIT 10

-- 노드 생성/업데이트
MERGE (p:Problem {id: 1}) SET p.question = '...'
```

---

## 2. 이 프로젝트의 데이터 구조

### 전체 그래프 모델 다이어그램

```
                    ┌──────────────┐
                    │   Problem    │
                    │─────────────│
                    │ id          │
                    │ question    │
                    │ answer      │
                    │ grade       │
                    │ school_level│
                    │ type        │
                    │ level       │
                    │ embedding[] │  ← 3072차원 벡터
                    └──────┬──────┘
                           │
               ┌───────────┼───────────┐
               │           │           │
          [HAS_TAG]   [MAIN_TAG]  [BELONGS_TO_GROUP]
               │           │           │
               ▼           ▼           ▼
          ┌─────────┐ ┌─────────┐ ┌──────────────┐
          │   Tag   │ │   Tag   │ │ ProblemGroup │
          │─────────│ │─────────│ │──────────────│
          │ id      │ │ id      │ │ id           │
          │ name    │ │ name    │ │ instruction  │
          │category │ │category │ │ source_id    │
          │parent_id│ └─────────┘ └──────┬───────┘
          └────┬────┘                    │
               │                    [HAS_SOURCE]
          [CHILD_OF]                     │
               │                         ▼
               ▼                   ┌──────────┐
          ┌─────────┐              │  Source  │
          │  Tag    │              │──────────│
          │(parent) │              │ id       │
          └─────────┘              │ name     │
                                   │ exam_type│
                                   │ year     │
                                   └────┬─────┘
                                        │
                                  [FROM_SCHOOL]
                                        │
                                        ▼
                                  ┌──────────┐
                                  │  School  │
                                  │──────────│
                                  │ id       │
                                  │ kor_name │
                                  │ city     │
                                  └──────────┘
```

### 노드 레이블 상세

| 레이블 | 개수 (약) | 주요 속성 | 설명 |
|--------|----------|-----------|------|
| `Problem` | ~100,000 | `id`, `embedding`, `grade`, `school_level` | 수학 문제. 3072차원 벡터 임베딩 보유 |
| `Tag` | ~16,650 | `id`, `name`, `category`, `parent_id` | 수학 개념 태그. depth1~5 계층 구조 |
| `ProblemGroup` | 수만 | `id`, `instruction` | 지문이나 그룹 단위 묶음 |
| `Source` | 수천 | `id`, `name`, `exam_type`, `year` | 출제 출처 (시험지, 교재 등) |
| `School` | 수백 | `id`, `kor_name`, `city` | 학교 정보 |

### 관계 타입 상세

| 관계 타입 | 방향 | 설명 |
|-----------|------|------|
| `HAS_TAG` | Problem → Tag | 문제가 해당 태그를 가짐. 평균 9.6개/문제 |
| `MAIN_TAG` | Problem → Tag | 문제의 대표(메인) 카테고리 태그 |
| `CHILD_OF` | Tag → Tag | 하위 태그가 상위 태그의 자식임 |
| `BELONGS_TO_GROUP` | Problem → ProblemGroup | 문제가 속한 지문 그룹 |
| `HAS_SOURCE` | ProblemGroup → Source | 그룹의 출처 정보 |
| `FROM_SCHOOL` | Source → School | 출처가 속한 학교 |

### 태그 계층 구조 (depth1 ~ depth5)

태그는 5단계 계층으로 구성됩니다. depth가 높을수록 더 세분화된 개념입니다.

```
depth1: 중학수학
  └── depth2: 중학교 1학년
        └── depth3: 수와 연산
              └── depth4: 유리수와 소수
                    └── depth5: 순환소수를 분수로 변환
```

`tag.category` 필드에 `'depth1'`, `'depth2'`, ..., `'depth5'` 값이 저장됩니다.

두 문제가 depth5 태그를 공유한다면 매우 유사한 개념을 다루는 것이고, depth1만 공유한다면 같은 학년 수학이지만 다른 단원일 수 있습니다.

---

## 3. Docker로 Neo4j 구동

### 사전 요구사항

- Docker 및 Docker Compose 설치
- 프로젝트 루트에 `.env` 파일 설정

### .env 파일 설정

`.env.example`을 복사하여 `.env`를 만들고 Neo4j 비밀번호를 설정합니다.

```bash
cp .env.example .env
```

`.env` 파일에서 다음 항목을 확인합니다.

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password
```

### Docker Compose 파일 설명

```yaml
# docker-compose.yml
services:
  neo4j:
    image: neo4j:5.26-community        # Neo4j 5.26 Community Edition
    container_name: math-similarity-neo4j
    ports:
      - "7474:7474"                     # Neo4j Browser (Web UI)
      - "7687:7687"                     # Bolt 프로토콜 (Python 드라이버가 사용)
    volumes:
      - ./data/neo4j/data:/data         # 그래프 데이터 영구 저장
      - ./data/neo4j/logs:/logs         # 로그 저장
    environment:
      - NEO4J_AUTH=neo4j/changeme       # 인증 정보 (.env에서 오버라이드 가능)
      - NEO4J_server_memory_heap_initial__size=2g
      - NEO4J_server_memory_heap_max__size=3g
      - NEO4J_server_memory_pagecache_size=1g  # 총 6GB RAM 권장
    restart: unless-stopped
    healthcheck:
      # 15초마다 7474 포트로 HTTP 헬스체크
      test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:7474 || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 5
```

### 구동 방법

```bash
# Neo4j 컨테이너 시작 (백그라운드)
docker compose up -d neo4j

# 상태 확인
docker compose ps

# 로그 확인
docker compose logs -f neo4j

# 컨테이너 중지
docker compose stop neo4j
```

### 브라우저에서 접속

Neo4j가 시작된 후 웹 브라우저에서 `http://localhost:7474` 에 접속하면 **Neo4j Browser**를 사용할 수 있습니다. Cypher 쿼리를 직접 입력하고 결과를 시각적으로 확인할 수 있습니다.

- 접속 URL: `bolt://localhost:7687`
- 사용자명: `neo4j`
- 비밀번호: `.env`에 설정한 값

### Bolt 프로토콜이란

Python `neo4j` 라이브러리는 7687 포트의 **Bolt** 프로토콜로 통신합니다. Bolt는 Neo4j 전용 바이너리 프로토콜로, HTTP(7474)보다 빠릅니다. `Neo4jClient`의 `connect()` 메서드가 이 포트로 연결합니다.

---

## 4. 데이터 마이그레이션 과정

마이그레이션 스크립트(`scripts/migrate_to_neo4j.py`)는 두 개의 소스 DB에서 데이터를 읽어 Neo4j에 적재합니다.

### 데이터 소스

```
SQLite (data/math_problems.db)    MySQL (problem_bank)
  └── problems 테이블                ├── tag 테이블
       ├── id, question, answer      ├── problem_tag_bind 테이블
       ├── grade, school_level       ├── problem_group 테이블
       ├── group_id                  ├── source_info 테이블
       └── full_text_vector (BLOB)   └── school 테이블
```

### 실행 방법

```bash
# 기본 실행 (기존 데이터 유지, 추가만)
python scripts/migrate_to_neo4j.py

# 전체 초기화 후 재적재
python scripts/migrate_to_neo4j.py --clear

# 옵션 설명
python scripts/migrate_to_neo4j.py \
  --sqlite-db data/math_problems.db \   # SQLite 경로 (기본값)
  --batch-size 500 \                    # 한 번에 처리할 레코드 수 (기본값: 500)
  --skip-vectors \                      # 임베딩 벡터 제외 (테스트용, 빠름)
  --clear                               # 시작 전 Neo4j 전체 초기화
```

### 9단계 마이그레이션 흐름

```
Step 1  MySQL에서 활성 태그 로드
          └── desc='deactivated'인 태그 + 그 자식 태그 전부 제외
          └── 활성 태그 ID 집합(active_tag_ids) 생성

Step 2  Tag 노드 생성 (Neo4j MERGE)
          └── id, name, category, parent_id 속성 저장

Step 3  CHILD_OF 관계 생성
          └── child.parent_id를 따라 Tag→Tag 관계 연결

Step 4  Problem 노드 생성 (SQLite에서)
          └── id, question, answer, grade, school_level 등 저장
          └── full_text_vector BLOB → float[] 변환 후 embedding 속성 저장

Step 5  HAS_TAG 관계 생성 (MySQL problem_tag_bind에서)
          └── 약 1,550만 행 스캔 후 활성 태그+SQLite 문제만 필터
          └── Problem→Tag MERGE

Step 6  MAIN_TAG 관계 생성
          └── Problem.main_category_tag_id → Tag 연결

Step 7  ProblemGroup, Source, School 노드 + 관계 생성
          └── BELONGS_TO_GROUP, HAS_SOURCE, FROM_SCHOOL 관계 연결

Step 8  인덱스 생성
          └── 벡터 인덱스: problem_embedding (3072차원, cosine)
          └── 일반 인덱스: grade, school_level, group_id, tag.category 등

Step 9  검증
          └── 각 노드/관계 수 카운트 출력
```

### Step 1: 비활성 태그 필터링 로직

`tag.desc = 'deactivated'`로 표시된 태그는 폐기된 태그입니다. 단순히 해당 태그만 제외하는 것이 아니라, **그 태그의 모든 자식 태그도 재귀적으로 제외**합니다.

```python
def get_excluded_tag_ids(cursor) -> set:
    # 1. 직접 비활성화된 태그 ID 수집
    cursor.execute("SELECT id FROM tag WHERE `desc` = 'deactivated'")
    deactivated_ids = {row[0] for row in cursor.fetchall()}

    # 2. 부모→자식 맵 구성
    cursor.execute("SELECT id, parent_id FROM tag WHERE parent_id IS NOT NULL")
    children_map = defaultdict(list)

    # 3. DFS로 모든 자식 재귀 수집
    def collect_descendants(tid):
        for child_id in children_map.get(tid, []):
            if child_id not in excluded:
                excluded.add(child_id)
                collect_descendants(child_id)
```

예를 들어 depth2 태그가 비활성화되면 그 아래 depth3, depth4, depth5 태그도 모두 Neo4j에서 제외됩니다.

### Step 4: 임베딩 벡터 변환

SQLite의 `full_text_vector` 컬럼은 OpenAI `text-embedding-3-large` 모델이 생성한 3072차원 벡터를 numpy float32 BLOB 형태로 저장합니다. 마이그레이션 시 Python에서 float 리스트로 변환합니다.

```python
vec_blob = p.pop("full_text_vector")
if vec_blob and not skip_vectors:
    p["embedding"] = np.frombuffer(vec_blob, dtype=np.float32).tolist()
```

Neo4j에 저장된 `embedding` 속성은 이후 벡터 인덱스(Step 8)의 대상이 됩니다.

### Step 5: 대용량 관계 처리

`problem_tag_bind` 테이블은 약 1,550만 행입니다. 메모리 초과를 피하기 위해 `fetchmany(10000)`으로 스트리밍 처리합니다.

```python
mysql_cursor.execute("SELECT problem_id, tag_id FROM problem_tag_bind")
while True:
    rows = mysql_cursor.fetchmany(10000)  # 10,000행씩 읽기
    if not rows:
        break
    for r in rows:
        # SQLite에 있는 문제 AND 활성 태그인 경우만 수집
        if r[0] in sqlite_problem_ids and r[1] in active_tag_ids:
            bindings.append({"problem_id": r[0], "tag_id": r[1]})
```

### Step 8: 벡터 인덱스 생성

Neo4j 5.x에서는 벡터 유사도 검색을 위한 전용 인덱스를 생성합니다.

```cypher
CREATE VECTOR INDEX problem_embedding IF NOT EXISTS
FOR (p:Problem) ON (p.embedding)
OPTIONS {indexConfig: {
    `vector.dimensions`: 3072,
    `vector.similarity_function`: 'cosine'
}}
```

- `vector.dimensions`: 벡터의 차원수. OpenAI text-embedding-3-large와 일치해야 합니다.
- `vector.similarity_function`: `'cosine'`(코사인 유사도) 또는 `'euclidean'`.
- `IF NOT EXISTS`: 재실행 안전성 보장.

### 마이그레이션 완료 후 기대 출력

```
=== Verification ===
  Problems: 100,000
  Tags: 16,650
  HAS_TAG: 960,000
  CHILD_OF: 16,000
  MAIN_TAG: 100,000
  BELONGS_TO_GROUP: 85,000
  ProblemGroups: 40,000
  Sources: 3,200
  Schools: 250

Migration complete! Total time: 1800.0s
```

---

## 5. 검색 로직 상세 설명

### 전체 구조

`GraphSearchService.search_hybrid()`는 두 가지 모드로 동작합니다.

```
search_hybrid(problem_id, question, solution, ...)
    │
    ├── problem_id가 있는 경우 → Mode A (Problem-ID 기반)
    │     └── _search_mode_a(): 단일 Cypher 쿼리로 모두 처리
    │
    └── problem_id가 없는 경우 → Mode B (자유 텍스트 기반)
          ├── EmbeddingService.embed_text()  → 쿼리 벡터 생성
          └── _search_mode_b(): 벡터 검색 + Python 후처리
```

### Mode A: Problem-ID 기반 검색

ID가 주어진 경우, 해당 문제의 임베딩과 태그가 이미 Neo4j에 있으므로 **단일 Cypher 쿼리**로 모든 계산을 완료합니다.

#### 전체 Cypher 쿼리

```cypher
-- 1. 쿼리 문제 노드와 임베딩 벡터를 가져온다
MATCH (query:Problem {id: $query_id})
WITH query, query.embedding AS qvec

-- 2. 쿼리 문제의 태그를 수집한다
OPTIONAL MATCH (query)-[:HAS_TAG]->(qt:Tag)
WITH query, qvec,
     collect({id: qt.id, category: qt.category}) AS query_tags

-- 3. 벡터 인덱스로 유사한 후보를 candidate_k(=top_k*3)개 찾는다
CALL db.index.vector.queryNodes('problem_embedding', $candidate_k, qvec)
YIELD node AS candidate, score AS vec_score
-- 4. 자기 자신 제외 + grade/school_level 필터 적용
WHERE candidate.id <> $query_id
  AND ($grade IS NULL OR candidate.grade = $grade)
  AND ($school_level IS NULL OR candidate.school_level = $school_level)

-- 5. 각 후보의 태그를 수집한다
WITH candidate, vec_score, query_tags
OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)
WITH candidate, vec_score, query_tags,
     collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

-- 6. tag_score를 계산한다 (depth 가중치 기반 Jaccard)
WITH candidate, vec_score, query_tags, cand_tags,
     CASE
       WHEN size(query_tags) = 0 THEN 0.0  -- 쿼리 태그가 없으면 0
       WHEN size([qt IN query_tags WHERE qt.id IN [ct IN cand_tags | ct.id]]) = 0 THEN 0.0
       -- 공통 태그의 가중치 합 / 쿼리 태그의 가중치 합
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
             ...
           END
         )
       )
     END AS tag_score

-- 7. 최종 점수 계산 후 상위 top_k개 반환
WITH candidate, vec_score, tag_score, cand_tags, query_tags,
     ($alpha * vec_score + $beta * tag_score) AS final_score,
     [ct IN cand_tags WHERE ct.id IN [qt IN query_tags | qt.id]] AS shared_tags_info
ORDER BY final_score DESC
LIMIT $top_k

RETURN candidate.id AS id,
       round(final_score, 4) AS score,
       round(vec_score, 4) AS vector_score,
       round(tag_score, 4) AS graph_score,
       [st IN shared_tags_info | st.name] AS shared_tags,
       false AS graph_score_inferred  -- Mode A는 실제 태그 기반이므로 추정 아님
```

#### 쿼리 흐름 한 줄씩 설명

| 단계 | 코드 | 설명 |
|------|------|------|
| 1 | `MATCH (query:Problem {id: $query_id})` | 쿼리 문제를 ID로 정확히 찾는다 |
| 2 | `query.embedding AS qvec` | 이 문제의 3072차원 벡터를 가져온다 |
| 3 | `OPTIONAL MATCH (query)-[:HAS_TAG]->(qt:Tag)` | 쿼리 문제의 모든 태그를 찾는다. OPTIONAL이므로 태그가 없어도 진행 |
| 4 | `collect(...)  AS query_tags` | 태그들을 리스트로 묶는다 |
| 5 | `db.index.vector.queryNodes(...)` | 벡터 인덱스에서 qvec와 가장 유사한 candidate_k개를 찾는다 |
| 6 | `WHERE candidate.id <> $query_id` | 자기 자신은 제외 |
| 7 | `$grade IS NULL OR candidate.grade = $grade` | grade 필터. NULL이면 전체 |
| 8 | `OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)` | 후보의 태그를 수집 |
| 9 | `reduce(s = 0.0, ...)` | 공통 태그 가중치 합산 (depth5=5점, depth1=1점) |
| 10 | `/ toFloat(reduce(s = 0.0, qt IN query_tags ...))` | 쿼리 태그 가중치 합으로 나눠 0~1 정규화 |
| 11 | `$alpha * vec_score + $beta * tag_score` | 최종 하이브리드 점수 계산 |
| 12 | `ORDER BY final_score DESC LIMIT $top_k` | 상위 결과만 반환 |

`candidate_k = top_k * 3`으로 후보를 3배 많이 가져오는 이유는, 벡터만으로 뽑은 후보 중 일부는 tag_score가 낮아 순위가 밀릴 수 있기 때문입니다. 더 넓은 후보군에서 최종 점수로 다시 정렬하여 정밀도를 높입니다.

### Mode B: 텍스트 기반 검색

문제 ID 없이 자유 텍스트만 주어진 경우입니다. 텍스트의 임베딩은 만들 수 있지만, 이 텍스트에 해당하는 태그는 Neo4j에 없습니다. 따라서 **태그를 추론(infer)**합니다.

#### Step 1: 텍스트 임베딩 생성

```python
full_text = f"{question}\n{solution}" if solution else question
query_embedding = await self.embedding_service.embed_text(full_text)
```

OpenAI `text-embedding-3-large` API를 호출하여 3072차원 벡터를 만듭니다.

#### Step 2: 벡터 검색 Cypher

```cypher
-- 임베딩 벡터로 후보 검색 (Mode A의 3~8단계와 유사)
CALL db.index.vector.queryNodes('problem_embedding', $candidate_k, $query_embedding)
YIELD node AS candidate, score AS vec_score
WHERE ($exclude_id IS NULL OR candidate.id <> $exclude_id)
  AND ($grade IS NULL OR candidate.grade = $grade)
  AND ($school_level IS NULL OR candidate.school_level = $school_level)

-- 후보의 태그를 함께 가져온다
WITH candidate, vec_score
OPTIONAL MATCH (candidate)-[:HAS_TAG]->(ct:Tag)
WITH candidate, vec_score,
     collect({id: ct.id, name: ct.name, category: ct.category}) AS cand_tags

ORDER BY vec_score DESC
LIMIT $candidate_k

RETURN candidate.id AS id,
       round(vec_score, 4) AS vector_score,
       cand_tags AS tags
```

#### Step 3: Python으로 태그 추론 (inferred tags)

벡터 검색 결과에서 **상위 10개 후보에 가장 많이 등장하는 태그**를 찾아 쿼리의 추정 태그로 사용합니다.

```python
# 상위 10개 후보의 depth3~5 태그 빈도 계산
top_candidates = records[:10]
tag_freq: Counter = Counter()
for c in top_candidates:
    for t in c.get("tags", []):
        if t["category"] in ("depth3", "depth4", "depth5"):  # 세밀한 태그만
            tag_freq[t["id"]] += 1

# 상위 5개 태그 중 2번 이상 등장한 것만 추정 태그로 선택
inferred_tags = {tid for tid, cnt in tag_freq.most_common(5) if cnt >= 2}
```

depth3~5만 사용하는 이유는, depth1~2는 너무 광범위해서(예: "고등수학") 변별력이 없기 때문입니다.

#### Step 4: graph_score 계산

```python
for c in records:
    cand_tag_ids = {t["id"] for t in c.get("tags", [])}
    shared_count = len(cand_tag_ids & inferred_tags)    # 교집합 크기
    graph_score = shared_count / max(len(inferred_tags), 1)  # 0~1 정규화

    final_score = alpha * c["vector_score"] + beta * graph_score
```

Mode B에서는 depth 가중치 없이 단순 비율로 계산합니다. inferred_tags가 추정값이므로 세밀한 가중치보다 단순한 방식이 더 적합합니다. 결과에 `graph_score_inferred: true` 플래그가 붙어 UI에서 "추정" 뱃지를 표시합니다.

### 최종 점수 계산: alpha × vector_score + beta × tag_score

```
final_score = α × vector_score + β × tag_score

기본값: α = 0.6, β = 0.4
조건:   α + β = 1.0 (UI 슬라이더가 이를 강제)
```

| 점수 | 범위 | 의미 |
|------|------|------|
| `vector_score` | 0.0 ~ 1.0 | 코사인 유사도. 임베딩 공간에서의 의미적 유사성 |
| `tag_score` | 0.0 ~ 1.0 | 태그 가중치 Jaccard. 수학 개념의 구조적 유사성 |
| `final_score` | 0.0 ~ 1.0 | 두 점수의 가중 합산 |

**alpha를 높이면**: 의미적으로 비슷한 문장 패턴을 가진 문제가 우선됩니다.
**beta를 높이면**: 같은 수학 단원/개념에 속하는 문제가 우선됩니다.

### tag_score의 depth별 가중치가 왜 있는가

```
depth5 공유: 5점  (예: "순환소수를 분수로 변환" 동일)
depth4 공유: 4점  (예: "유리수와 소수" 동일)
depth3 공유: 3점  (예: "수와 연산" 동일)
depth2 공유: 2점  (예: "중학교 1학년" 동일)
depth1 공유: 1점  (예: "중학수학" 동일)
```

태그 계층에서 깊은 레벨의 태그를 공유할수록 문제가 더 유사합니다. 단순 Jaccard(모든 태그에 동일한 가중치)는 depth1 같은 광범위한 태그를 공유해도 높은 점수를 주는 문제가 있습니다. 가중치를 통해 **세밀한 개념 일치를 더 크게 평가**합니다.

예시:

```
문제 A의 태그: [중학수학(d1), 1학년(d2), 수와연산(d3), 유리수(d4), 순환소수변환(d5)]

문제 B의 태그: [중학수학(d1), 1학년(d2), 수와연산(d3), 유리수(d4), 순환소수변환(d5)]
  → 공통 태그 가중치 합: 5+4+3+2+1 = 15
  → 쿼리 태그 가중치 합: 5+4+3+2+1 = 15
  → tag_score = 15/15 = 1.0  (완벽히 같은 개념)

문제 C의 태그: [중학수학(d1), 1학년(d2)]
  → 공통 태그 가중치 합: 2+1 = 3
  → 쿼리 태그 가중치 합: 5+4+3+2+1 = 15
  → tag_score = 3/15 = 0.2  (같은 학년이지만 다른 단원)
```

---

## 6. API 전체 흐름

### Graph 검색 요청 흐름

```
프론트엔드 (browser)
    │
    │  POST /api/search/compare
    │  {
    │    question: "...",
    │    problem_id: 12345,
    │    graph_enabled: true,
    │    graph_alpha: 0.6,
    │    graph_beta: 0.4
    │  }
    │
    ▼
FastAPI (app/main.py)
    │  search_compare() 또는 search_graph()
    │  neo4j_client.driver 연결 확인
    │
    ▼
GraphSearchService.search_hybrid()
    │  (graph_search_service.py)
    │
    ├── problem_id 있음? ──YES──> _search_mode_a()
    │                              Neo4j에 단일 Cypher 쿼리 실행
    │
    └── problem_id 없음? ──YES──> EmbeddingService.embed_text()
                                   OpenAI API 호출 → 3072d 벡터
                                  _search_mode_b()
                                   Neo4j 벡터 검색 → Python 태그 추론
    │
    ▼
_enrich_from_sqlite(results)
    │  SQLite에서 question, solution, tag_ids 등 상세 정보 보완
    │  (Neo4j에는 저장 용량 절약을 위해 일부 필드만 저장)
    │
    ▼
반환: [
  {
    id: 67890,
    score: 0.8234,         ← final_score (α*vector + β*tag)
    vector_score: 0.9012,  ← 벡터 코사인 유사도
    graph_score: 0.6800,   ← 태그 구조 유사도
    shared_tags: ["순환소수변환", "유리수"],
    graph_score_inferred: false,
    search_type: "graph",
    question: "...",       ← SQLite에서 보충
    solution: "...",
    ...
  }
]
    │
    ▼
FastAPI → JSON 응답
    │
    ▼
프론트엔드: Graph 컬럼에 결과 카드 렌더링
```

### app/main.py에서의 처리

```python
@app.post("/api/search/graph")
async def search_graph(req: SearchRequest):
    # Neo4j 연결 상태 확인
    if not neo4j_client.driver:
        return {"error": "Neo4j not connected", "results": []}

    results = await graph_search.search_hybrid(
        problem_id=req.problem_id,     # None이면 Mode B
        question=req.question,
        solution=req.solution,
        top_k=req.top_k,
        alpha=req.graph_alpha,         # 프론트엔드 슬라이더에서 옴
        beta=req.graph_beta,
        grade=req.grade,
        school_level=req.school_level,
        exclude_id=req.exclude_id,
    )
    return {"results": results}
```

`/api/search/compare`는 legacy, improved, reranked, graph를 `asyncio.gather()`로 **병렬 실행**합니다. graph 검색은 `graph_enabled=true`일 때만 포함됩니다.

### Neo4jClient 내부

```python
class Neo4jClient:
    def connect(self):
        # .env에서 읽은 환경변수로 Bolt 연결
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._driver.verify_connectivity()  # 연결 즉시 검증

    def execute_query(self, query: str, **params) -> list[dict]:
        with self._driver.session() as session:
            result = session.run(query, **params)  # 파라미터 바인딩으로 Injection 방지
            return [dict(record) for record in result]  # Record → dict 변환
```

세션은 매 쿼리마다 `with` 문으로 열고 닫습니다. 연결 풀링은 Neo4j Python 드라이버가 내부적으로 관리합니다.

---

## 7. 프론트엔드 UI 설명

### Graph DB 관련 UI 요소

#### 체크박스: Graph DB 검색 활성화

```html
<input type="checkbox" id="graph-enabled">
```

체크하면 `graph-alpha-item`과 `graph-beta-item` 슬라이더가 표시됩니다. 검색 요청 시 `graph_enabled: true`가 전송되고, 결과가 오면 Graph 컬럼이 나타납니다.

#### 슬라이더: alpha(α)와 beta(β)

```javascript
// graph-alpha 슬라이더 조작 시 beta를 자동 보완
$('graph-alpha').addEventListener('input', () => {
    const alpha = parseFloat($('graph-alpha').value);
    const beta = Math.round((1 - alpha) * 100) / 100;  // α + β = 1 강제
    $('graph-beta').value = beta;
    $('graph-alpha-val').textContent = alpha.toFixed(2);
    $('graph-beta-val').textContent = beta.toFixed(2);
});
```

alpha 슬라이더를 움직이면 beta는 `1 - alpha`로 자동 계산됩니다. 두 값의 합이 항상 1.0을 유지합니다.

#### 결과 카드의 점수 표시

Graph 컬럼의 카드는 두 가지 점수를 표시합니다.

```javascript
// G: {graph_score} 뱃지 (태그 유사도)
const graphScoreHtml = isGraph ? `
    <span class="graph-score-display">
        G: ${(result.graph_score ?? 0).toFixed(2)}
        ${result.graph_score_inferred
            ? '<span class="inferred-badge">추정</span>'  // Mode B일 때 표시
            : ''}
    </span>` : '';

// 벡터 유사도 점수 (반투명하게 표시)
const vectorScoreHtml = isGraph ? `
    <span class="score-badge ${scoreClass}" style="opacity:0.65; font-size:10px;"
          title="벡터 유사도">${scoreStr}</span>` : `...`;
```

카드에서 보이는 점수:
- `G: 0.68` — 태그 구조 유사도(graph_score). 메인으로 표시
- `0.8234` — final_score(벡터+태그 합산). 반투명하게 보조 표시
- `추정` 뱃지 — Mode B(텍스트 기반)에서 태그를 추론한 경우

#### 공유 태그 뱃지

```javascript
const sharedTagsHtml = isGraph && result.shared_tags && result.shared_tags.length > 0 ? `
    <div>
        ${result.shared_tags.map(t =>
            `<span class="shared-tag-badge">${escapeHtml(t)}</span>`
        ).join(' ')}
    </div>` : '';
```

쿼리 문제와 결과 문제가 공유하는 태그 이름이 뱃지로 표시됩니다. 왜 이 문제가 유사하다고 판단됐는지 직관적으로 확인할 수 있습니다.

#### 컬럼 토글 버튼

```javascript
function setGraphColumnVisible(visible) {
    columnAvailable.graph = visible;
    columnVisible.graph = visible;
    const btn = document.querySelector('.toggle-btn-graph');
    if (btn) {
        btn.style.display = visible ? '' : 'none';
        btn.classList.toggle('active', visible);
    }
    updateColumnLayout();  // 1~4열 그리드 자동 조절
}
```

Graph 검색이 활성화된 경우에만 상단 토글 버튼이 나타납니다. 버튼을 클릭하면 해당 컬럼만 숨기고 나머지 컬럼이 넓어집니다.

---

## 8. 실제 Cypher 쿼리 예시

Neo4j Browser(`http://localhost:7474`)에서 직접 실행해볼 수 있는 쿼리들입니다.

### 문제 하나의 태그 확인

```cypher
MATCH (p:Problem {id: 12345})-[:HAS_TAG]->(t:Tag)
RETURN p.id, t.name, t.category
ORDER BY t.category
```

### 특정 태그를 공유하는 문제 찾기

```cypher
MATCH (p1:Problem {id: 12345})-[:HAS_TAG]->(t:Tag {category: 'depth5'})
MATCH (p2:Problem)-[:HAS_TAG]->(t)
WHERE p2.id <> 12345
RETURN DISTINCT p2.id, t.name, t.category
LIMIT 20
```

### 태그 계층 탐색 (depth5에서 depth1까지 올라가기)

```cypher
MATCH path = (t:Tag {name: '순환소수변환'})-[:CHILD_OF*]->(root:Tag)
RETURN [node IN nodes(path) | node.name] AS hierarchy
```

### 두 문제의 공통 태그와 점수 직접 계산

```cypher
MATCH (p1:Problem {id: 12345})-[:HAS_TAG]->(t:Tag)
MATCH (p2:Problem {id: 67890})-[:HAS_TAG]->(t)
WITH collect(DISTINCT t.name) AS shared_tags,
     collect(DISTINCT t.category) AS shared_categories,
     count(DISTINCT t) AS shared_count
RETURN shared_tags, shared_categories, shared_count
```

### 그래프 순회로 유사 문제 찾기 (벡터 없이)

이것은 `search_by_graph_traversal()` 메서드의 쿼리입니다.

```cypher
MATCH (query:Problem {id: $problem_id})-[:HAS_TAG]->(qt:Tag)
WHERE qt.category IN ['depth3', 'depth4', 'depth5']

MATCH (other:Problem)-[:HAS_TAG]->(qt)
WHERE other.id <> $problem_id

WITH other,
     collect(DISTINCT qt.name) AS shared_tags,
     count(DISTINCT qt) AS shared_count,
     -- depth별 가중치 합산
     sum(CASE qt.category
       WHEN 'depth5' THEN 5
       WHEN 'depth4' THEN 4
       WHEN 'depth3' THEN 3
       ELSE 1
     END) AS weighted_count
ORDER BY weighted_count DESC
LIMIT 10

RETURN other.id AS id, shared_tags, shared_count, weighted_count AS score
```

### 벡터 유사도 검색 직접 실행

```cypher
-- 특정 문제와 벡터적으로 유사한 상위 5개 문제
MATCH (query:Problem {id: 12345})
CALL db.index.vector.queryNodes('problem_embedding', 5, query.embedding)
YIELD node AS candidate, score
WHERE candidate.id <> 12345
RETURN candidate.id, score
ORDER BY score DESC
```

### 인덱스 목록 확인

```cypher
SHOW INDEXES
```

### 노드/관계 수 확인

```cypher
MATCH (p:Problem) RETURN count(p) AS problems
UNION ALL
MATCH (t:Tag) RETURN count(t) AS tags
UNION ALL
MATCH ()-[r:HAS_TAG]->() RETURN count(r) AS has_tag_rels
```

---

## 9. 성능 관련 팁

### 인덱스 전략

이 프로젝트에서 생성하는 인덱스:

| 인덱스 이름 | 대상 | 용도 |
|-------------|------|------|
| `problem_embedding` | `Problem.embedding` | 벡터 ANN 검색 |
| `problem_grade` | `Problem.grade` | grade 필터 |
| `problem_school_level` | `Problem.school_level` | school_level 필터 |
| `problem_group_id` | `Problem.group_id` | 그룹 조회 |
| `tag_category` | `Tag.category` | depth 필터링 |
| `tag_parent` | `Tag.parent_id` | 계층 순회 |

`Problem` 노드의 `id`는 MERGE 구문으로 생성되어 자동으로 고유 인덱스가 생깁니다.

### candidate_k = top_k * 3 전략

```python
candidate_k = top_k * 3  # 예: top_k=10이면 30개 후보
```

벡터 ANN(Approximate Nearest Neighbor) 검색은 빠르지만 정확하지 않을 수 있습니다. 특히 필터 조건(grade, school_level)이 있으면 실제 반환 수가 줄어듭니다. 3배 더 많이 가져와서 tag_score 기반으로 재정렬하면 최종 품질이 높아집니다.

### batch-size 조절

```bash
# 메모리가 충분할 때 (8GB+): 배치 크기 늘려 마이그레이션 속도 향상
python scripts/migrate_to_neo4j.py --batch-size 1000

# 메모리 제한 환경: 배치 크기 줄이기
python scripts/migrate_to_neo4j.py --batch-size 200
```

Neo4j UNWIND 배치 작업에서 배치 크기가 클수록 트랜잭션 오버헤드가 줄어 빠르지만, 메모리 사용량이 증가합니다. 500이 일반적인 균형점입니다.

### 메모리 설정

`docker-compose.yml`의 Neo4j 메모리 설정:

```yaml
- NEO4J_server_memory_heap_initial__size=2g  # JVM 힙 초기 크기
- NEO4J_server_memory_heap_max__size=3g      # JVM 힙 최대 크기
- NEO4J_server_memory_pagecache_size=1g      # 디스크 페이지 캐시
```

- **heap**: Cypher 쿼리 처리, 객체 생성에 사용
- **pagecache**: 그래프 데이터(노드, 관계)를 메모리에 캐시

100K 문제 + 3072d 벡터의 경우 총 6GB(heap 3GB + pagecache 1GB + OS 여유)가 권장됩니다. 시스템 RAM이 부족하면 `pagecache_size`를 먼저 줄이세요.

### --skip-vectors 옵션 활용

태그 관련 로직만 테스트할 때는 벡터 없이 빠르게 마이그레이션할 수 있습니다.

```bash
python scripts/migrate_to_neo4j.py --skip-vectors
```

벡터 없이 마이그레이션하면 `problem_embedding` 인덱스가 생성되지 않으므로 `db.index.vector.queryNodes()` 호출 시 오류가 발생합니다. `search_by_graph_traversal()`(순수 태그 기반 탐색)은 벡터 없이도 동작합니다.

### MERGE vs CREATE

마이그레이션 스크립트는 모든 노드/관계 생성에 `MERGE`를 사용합니다.

```cypher
MERGE (p:Problem {id: p.id})   -- 있으면 업데이트, 없으면 생성
SET p.question = ...
```

`CREATE`는 무조건 생성하므로 재실행 시 중복 데이터가 생깁니다. `MERGE`는 멱등성(idempotent)을 보장하여 `--clear` 없이도 재실행이 안전합니다.

### 검색 레이턴시 기대값

로컬 환경(SSD, 8GB RAM) 기준:

| 검색 유형 | 예상 레이턴시 |
|-----------|---------------|
| Mode A (벡터 + 태그, 단일 Cypher) | 50~200ms |
| Mode B (텍스트 임베딩 포함) | 500ms~2s (OpenAI API 포함) |
| 순수 그래프 탐색 (`search_by_graph_traversal`) | 20~100ms |

벡터 인덱스 warm-up: Neo4j 재시작 후 첫 벡터 쿼리는 인덱스를 메모리에 로드하므로 느릴 수 있습니다. 몇 번 쿼리 후 pagecache에 올라오면 정상 속도가 됩니다.
