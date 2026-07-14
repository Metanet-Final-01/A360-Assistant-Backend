# 관측 DB 데이터 사전 (Observability Data Dictionary)

> 운영·감사·비용·장애추적을 위한 관측 전용 공유 DB(Neon)의 테이블 레퍼런스. 발표·실무 설명용.
> 보관기간·마스킹 **정책**은 [OBSERVABILITY_POLICY.md](OBSERVABILITY_POLICY.md), 여기서는 **"무슨 로그를 어디에 쌓고 무엇에 쓰는가"**를 정리한다.

앱은 `app/core/observability_db.py`가 `OBSERVABILITY_DATABASE_URL`로 앱 DB와 분리 연결(RPA-90). FK 없는 동형 테이블이며, 모든 쓰기는 **best-effort**(관측 DB 장애가 요청을 죽이지 않는다). 조회는 관리자 전용 API(`/api/admin/*`, `require_admin`).

## 1. 테이블 요약

| 테이블 | 무슨 로그인가 | 모델 | 채워지는 경로 |
|---|---|---|---|
| **llm_usage** | LLM 호출 1건당 토큰·비용·지연 (누가/어느 서브시스템/어느 모델 3축) | `app/models.py` LlmUsage | `app/core/llm.py::record_usage` (chat 래퍼 + Agent UsageCallbackHandler) |
| **audit_logs** | 변경성 요청(POST/PUT/PATCH/DELETE)의 "누가·무엇을" forensics | AuditLog | `app/core/http_logging.py::_record_audit` (미들웨어, threadpool) |
| **request_metrics** | **모든** 요청(GET 포함)의 지연·상태 (경로 정규화 `:id`) — 성능 집계 원천 | RequestMetric | `http_logging.py::_record_metric` (응답 후 BackgroundTask) |
| **turn_events** | 에이전트 턴의 노드 타임라인 (stage/error/done, 라우팅 근거·검색쿼리·검수위반) | TurnEvent | `app/api/sessions.py::_save_turn_events` (턴 종료 시 버퍼 일괄) |
| **rag_events** | RAG 파이프라인 단계별 소요·파라미터 (embed/bm25/rerank/hybrid + 설정 스냅샷) | RagEvent | `app/rag/observability.py::_persist_rag_event` (JSONL과 동시 중앙화) |
| **metrics_daily** | request_metrics 일별 롤업 (일자×method×path: calls·4xx·5xx·p50/p95) | MetricsDaily | `app/services/rollup.py::rollup_metrics_day` (APScheduler, 멱등 DELETE+INSERT) |
| **usage_daily** | llm_usage 일별 롤업 (일자×component×purpose×model: calls·토큰·비용) | UsageDaily | `rollup.py::rollup_usage_day` (APScheduler) |

## 2. 실무 활용 — "관리자가 이 테이블로 무엇을 답하나"

| 테이블 | 답할 수 있는 질문 |
|---|---|
| **llm_usage** | 세션별/사용자별 비용은? 임베딩 vs 리랭커 vs 챗 비용 비중은? 어느 모델이 제일 비싼가? |
| **audit_logs** | 누가 이 문서를 삭제/수정했나? 403/404 접근 시도가 있었나? (성공·실패 모두 기록) |
| **request_metrics** | 엔드포인트별 p95 지연은? 어느 API가 느린가? 에러율 추이는? |
| **turn_events** | 어떤 노드에서 몇 ms 걸렸나? 어디서 실패했나? 왜 edit로 라우팅됐나? |
| **rag_events** | 이 검색이 어떤 chunk_size·RRF로 돌았나? 느린 단계는 embed냐 rerank냐? 실패한 검색은? |
| **metrics_daily / usage_daily** | Ops 대시보드의 날짜별 성능/비용 피벗 — raw retention 이후에도 장기 보관 |

## 3. 거버넌스 재구성 — "누가·언제·무슨 근거·얼마 비용"

`audit_logs`·`turn_events`·`rag_events`는 **`request_id`로 한 턴을 묶어** 누가(user_id)·언제·무슨 근거(라우팅 reason)·어떤 검색 흐름까지 재구성 가능하다. **끊기는 곳은 "얼마 비용"** — 아래 갭 참고.

## 4. 알려진 갭 (2026-07-14 감사 실측)

| # | 갭 | Severity |
|---|---|---|
| G1 | **비용을 request/turn 단위로 귀속 불가** — `llm_usage`에 `request_id` 없어 audit/turn/rag와 조인 키가 없다. `session_id`로만 연결 → "이 **턴**이 얼마"에 답 못 하고 세션 총액까지만 | **P1** |
| G2 | **cost_usd ~15% 결측** — 가격 env(`LLM_*_COST_PER_1M`) 설정 이전 챗 호출은 토큰만 있고 비용 null → 과거 비용 리포트 과소 집계 | **P1** |
| G3 | **audit "누가"의 절반이 익명** — 로그인 사용자도 일부 요청에서 user_id null(미들웨어 JWT 디코드 갭) | P2 |
| G4 | **관측 DB 잔여 PII** — 마스킹 배포 이전 `turn_events.detail` 행에 평문 이메일 잔존(백필 안 됨). 마스킹은 신규 쓰기만 적용 | P2 |
| G5 | **당일 대시보드 stale** — 롤업이 주기 배치라 당일 수치는 마지막 롤업 이후 유입분 누락. 실시간 감시엔 raw 조회 필요 | P2 |

## 5. 데이터 품질 (검증됨)

- **롤업 멱등·정합**: 마감된 날은 raw와 완전 일치, 재집계(DELETE+INSERT) 안전. 당일 불일치는 버그가 아니라 롤업 lag(G5).
- **안정성**: 관측 기간 5xx 0건, turn error율 낮음.
