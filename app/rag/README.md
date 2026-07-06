# A360 RAG 수집 파이프라인

Automation 360 패키지/액션 지식을 수집해 pgvector에 적재하는 파이프라인.

## 데이터 소스 3개

| 소스 | 방법 | 얻는 것 |
|---|---|---|
| 공식 문서 | Fluid Topics 공개 API (`/api/khub/...`) — HTML 스크레이핑 불필요 | 한국어 액션 설명, 사용 조건, 주의사항 |
| 패키지 JAR | Control Room에서 봇 export(패키지 포함) → zip 안의 JAR → 내부 `package.json` | 액션별 공식 스키마 (파라미터명/타입/필수/기본값/리턴) |
| 봇 JSON | Control Room Repository API | 실제 봇의 액션 조합 예시 (추천/생성 근거) |

## 사용 순서

```bash
# 1. 문서 크롤링 (기본: 명령 패널 전체 ~1300페이지. --contains로 부분 수집 가능)
python -m app.rag.pipeline crawl                        # 재시작 안전 (이어받기)
python -m app.rag.pipeline crawl --contains "Google Sheets"

# 2a. 패키지 스키마 추출 — 수동으로 받은 zip/jar
python -m app.rag.pipeline parse-jars path/to/export.zip

# 2b. 또는 Control Room API로 자동 (CR_URL/CR_USERNAME/CR_API_KEY 필요)
python -m app.rag.pipeline bots                          # 봇 목록+JSON 수집
python -m app.rag.pipeline export-packages --file-ids 123 456   # BLM export → JAR 스키마 자동 추출

# 3. 병합 → RAG 문서 생성 (data/ingest/rag_documents.jsonl)
python -m app.rag.pipeline build

# 4. 임베딩 + pgvector 적재
docker compose up -d db                                     # 5432 사용 중이면 DATABASE_PORT=5433
python -m app.rag.pipeline ingest                        # VOYAGE_API_KEY 필요
python -m app.rag.pipeline ingest --skip-embedding       # 임베딩 없이 텍스트만 적재

# 5. 검색 (CLI 또는 API)
python -m app.rag.pipeline search "구글시트에서 시트 활성화 어떻게 해?" --mode hybrid_rerank
# GET /api/rag/search?q=...&limit=5&mode=hybrid_rerank
```

## 하이브리드 검색 + Reranker (RPA-9)

`app/rag/retrieval/hybrid_search.py`가 pgvector 코사인 유사도 + OpenSearch BM25를
RRF(k=60)로 융합하고, Voyage `rerank-2.5-lite`로 최종 재정렬한다. `mode` 파라미터로
`vector`(기존 방식) / `hybrid`(RRF만) / `hybrid_rerank`(기본) 선택 가능.

- OpenSearch 로컬 컨테이너: `docker compose up -d opensearch`
- Graceful degradation: OpenSearch 장애 시 벡터 단독으로, `VOYAGE_API_KEY` 미설정 시
  RRF 순서 그대로 반환 — 절대 API가 죽지 않는다. 결과에 `reranked`/`rerank_fallback_reason`/
  `bm25_available` 필드로 실제 어느 단계까지 됐는지 표시됨.
- 청킹: `app/rag/build/chunk.py` (LangChain `RecursiveCharacterTextSplitter`,
  `CHUNK_SIZE=1200`/`CHUNK_OVERLAP=200` — 실제 데이터 EDA로 검증됨, `pipeline.py eda` 참고)

## Agent 연동

`app/services/rag.py::search_actions(query, k=5, source_types=None)` —
`docs/INTERFACES.md` 계약대로 Agent 담당(`app/agent/retrieval.py`)이 쓸 진입점.
반환 필드: `id/source_type/package_name/action_name/title/url/content/score`
(단, `doc_page`/`bot_example`은 특정 패키지에 매인 문서가 아니라 `package_name`/
`action_name`이 원래부터 `null` — 정상 동작).

## 디버그 콘솔

`GET /debug` (백엔드 기동 후 `http://localhost:8000/debug/debug.html`) —
vector/hybrid/hybrid_rerank 3단 비교, 서비스 상태(DB/OpenSearch/임베딩/리랭커) 점검,
`search_actions()` Agent 계약 필드 확인, 임의 HTTP 요청 전송, 파이프라인 실시간 로그를
한 화면에서 확인 가능. 로그 원본은 `app/rag/logs/*.jsonl` (날짜별, gitignore).

## 패키지 JAR 수동으로 얻는 법 (API 대신 UI로 할 때)

1. Control Room에서 대상 패키지들을 쓰는 더미 봇 생성 (public workspace로 check-in)
2. Automation 목록에서 봇 export — "Exclude bot packages"를 **체크하지 않음** (기본값이 포함)
3. Activity > Historical에서 zip 다운로드 → `parse-jars`에 zip 그대로 전달

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `EMBEDDING_PROVIDER` | `voyage` | `voyage` 또는 `openai` (Anthropic은 임베딩 API가 없어 Voyage 공식 권장) |
| `EMBEDDING_MODEL` | `voyage-3.5` / `text-embedding-3-small` | 다국어(한국어) 지원 모델 |
| `EMBEDDING_DIM` | `1024` / `1536` | rag_documents.embedding 차원 — 변경 시 테이블 재생성 필요 |
| `VOYAGE_API_KEY` / `OPENAI_API_KEY` | — | 임베딩 API 키 |
| `DATABASE_HOST/PORT/NAME/USERNAME/PASSWORD` | docker-compose 기본값 | pgvector Postgres 접속 정보 |
| `CR_URL` | — | Control Room URL (예: https://xxx.cloud.automationanywhere.digital) |
| `CR_USERNAME`, `CR_API_KEY`(또는 `CR_PASSWORD`) | — | Control Room 인증 (`bots`, `export-packages`에만 필요) |
| `OPENSEARCH_HOST` | `http://localhost:9200` | BM25 검색용 OpenSearch |
| `VOYAGE_API_KEY` | — | Reranker용 (없으면 RRF 순서 그대로 반환, graceful degradation) |
| `RRF_K` / `HYBRID_CANDIDATE_POOL_SIZE` / `HYBRID_RERANK_CANDIDATES` | `60` / `50` / `20` | 하이브리드 검색 튜닝값 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1200` / `200` | 청킹 크기 (EDA로 검증된 기본값) |

## RAG 문서 유형

- `action_schema` — 액션 1개당 1문서. JAR의 공식 스키마 + 매칭된 문서 설명. `metadata.schema`에 원본 스키마 JSON 보존 (봇 생성 시 파라미터 검증에 사용 가능).
- `package_overview` — 패키지 1개당 1문서 (액션 목록 요약).
- `doc_page` — 크롤링한 문서 페이지 1개당 1문서 (breadcrumbs 포함).
- `bot_example` — Control Room에서 수집한 실제 봇 1개당 1문서 (사용 패키지, 액션 순서, 파라미터 이름). 추천 시 "이런 조합으로 만든다"의 근거.
