"""횡단 관심사(AOP) 미들웨어 — 모든 HTTP 요청을 라우트 코드 손 안 대고 관측한다.

한 곳에서:
- request-id 발급 → 요청 안의 파이프라인 로그(embed_query 등)가 같은 id로 묶이고 응답
  헤더(X-Request-ID)로도 나간다.
- 요청/응답 구조화 로그(method·path·status·latency·user) — app/rag/observability로.
- 성능 측정(latency).
- 감사 로그(누가·무엇을) → 변경성 요청(POST/PUT/PATCH/DELETE)을 성공·실패 모두 audit_logs DB에
  (status_code로 구분). 조회(GET)는 로그로만 남긴다 (중요 이벤트만 DB).

user_id는 Authorization 헤더의 JWT를 디코드해 얻는다 (DB 조회 없이, best-effort) —
의존성 실행 순서/스레드풀 전파에 기대지 않아 견고하다.
"""

import logging
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

# /debug, /api/rag/logs/recent는 디버그 콘솔이 1.5초마다 스스로 폴링하는 경로라 제외한다
# (그러지 않으면 로그가 자기 자신의 폴링 기록으로 도배된다).
# 헬스 경로도 같은 병이다 (RPA-222): ALB(30초×AZ 2) + docker healthcheck(30초)면
# 인스턴스당 하루 8,640회 — 현재 request_metrics 일평균의 22배가 프로브 기록으로 찬다.
# prefix 매칭이라 "/health"가 /health/live까지 덮는다.
_SKIP_HTTP_LOG_PREFIXES = ("/api/rag/logs/recent", "/debug", "/health", "/api/health")
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 성능 메트릭(request_metrics)에서 제외 — CORS preflight 등 노이즈 (RPA-103)
_METRIC_SKIP_METHODS = {"OPTIONS", "HEAD"}

_UUID_SEGMENT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _normalize_path(path: str) -> str:
    """경로의 가변 세그먼트(UUID·숫자)를 플레이스홀더로 치환 — 메트릭 GROUP BY용 (RPA-103).

    /api/sessions/4b80…/turn → /api/sessions/:id/turn,
    /recommendations/3/export → /recommendations/:n/export.
    audit_logs는 forensics라 실제 경로를 유지하고, request_metrics만 정규화한다 —
    정규화 없이는 세션마다 다른 행이 돼 '엔드포인트별 p95' 집계(피벗)가 불가능하다.
    """
    parts = []
    for seg in path.split("/"):
        if _UUID_SEGMENT.match(seg):
            parts.append(":id")
        elif seg.isdigit():
            parts.append(":n")
        else:
            parts.append(seg)
    return "/".join(parts)[:255]


def _no_crlf(value: str) -> str:
    """CR/LF 제거 — 외부 입력(경로·쿼리)이 로그 라인을 위조하는 로그 인젝션 방지."""
    return value.replace("\r", "").replace("\n", "")


def _user_id_from_request(request: Request) -> str | None:
    """Authorization: Bearer JWT에서 user_id를 뽑는다 (DB 조회 없음, 실패 시 None)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        from app.core.security import decode_access_token

        return decode_access_token(auth[7:].strip())
    except Exception:  # noqa: BLE001 — 잘못된 토큰은 익명 취급
        return None


def _record_audit(request_id, user_id, method, path, status_code, latency_ms) -> None:
    """변경성 요청의 감사 행을 남긴다 (best-effort — 실패해도 요청엔 영향 없음)."""
    try:
        import uuid as _uuid

        from app import models
        from app.core.observability_db import observability_sessionmaker

        uid = None
        if user_id:
            try:
                uid = _uuid.UUID(user_id)
            except (ValueError, TypeError):
                uid = None
        # 관측 전용 DB(RPA-90) — 미설정/장애 시 아래 best-effort 경계에서 기록만 건너뛴다.
        with observability_sessionmaker()() as s:
            s.add(
                models.AuditLog(
                    request_id=request_id,
                    user_id=uid,
                    method=method,
                    path=path,
                    status_code=status_code,
                    latency_ms=latency_ms,
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001
        logger.warning("감사 로그 기록 실패: %s %s", method, path, exc_info=True)


def _record_metric(request_id, user_id, method, norm_path, status_code, latency_ms) -> None:
    """모든 요청의 성능 행(request_metrics)을 남긴다 (RPA-103, best-effort).

    감사와 달리 GET에도 붙으므로 응답 경로를 막으면 안 된다 — 호출부가
    BackgroundTask(응답 전송 후)로 돌려 조회 지연에 관측 DB 왕복이 얹히지 않게 한다.
    """
    try:
        import uuid as _uuid

        from app import models
        from app.core.observability_db import observability_sessionmaker

        uid = None
        if user_id:
            try:
                uid = _uuid.UUID(user_id)
            except (ValueError, TypeError):
                uid = None
        with observability_sessionmaker()() as s:
            s.add(
                models.RequestMetric(
                    request_id=request_id,
                    user_id=uid,
                    method=method,
                    path=norm_path,
                    status_code=status_code,
                    latency_ms=latency_ms,
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001
        logger.warning("요청 메트릭 기록 실패: %s %s", method, norm_path, exc_info=True)


def register_http_logging(app: FastAPI) -> None:
    @app.middleware("http")
    async def log_http_requests(request: Request, call_next):
        from app.rag.observability import log_event, new_request_id

        if request.url.path.startswith(_SKIP_HTTP_LOG_PREFIXES):
            return await call_next(request)

        # 이 HTTP 요청 안의 모든 파이프라인 로그(embed_query 등)를 이 id로 묶는다
        req_id = new_request_id()
        if not req_id:
            req_id = uuid.uuid4().hex[:16]
        user_id = _user_id_from_request(request)
        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()
        safe_path = _no_crlf(request.url.path)
        common = {
            "method": request.method,
            "path": safe_path,
            "query": _no_crlf(str(request.url.query)),
            "client": request.client.host if request.client else None,
            "user_id": user_id,
            "request_id": req_id,
            "started_at": started_at.isoformat(),
        }
        try:
            response = await call_next(request)
        except Exception as exc:
            # 전역 에러 핸들러(install_error_handlers)가 Exception까지 잡아 500 응답으로 바꾸므로
            # 여기(미들웨어 except)는 거의 안 탄다. 그 500 응답은 아래 감사 경로가 이미 기록한다.
            # 여기서 다시 _record_audit(=DB commit)을 시도하면, 장애 원인이 DB일 때 같은 DB로
            # 무제한 재시도해 스레드풀을 오래 점유할 수 있어(CodeRabbit) 감사는 남기지 않는다.
            log_event(
                "http_request",
                **common,
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                ended_at=datetime.now(timezone.utc).isoformat(),
            )
            raise

        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        log_event(
            "http_request",
            **common,
            status_code=response.status_code,
            status="ok" if response.status_code < 400 else "error",
            duration_ms=latency_ms,
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        response.headers["X-Request-ID"] = req_id

        # 감사: 변경성 요청은 성공·실패 모두 DB에 남긴다 — "누가 무엇을 시도했나"(403/404/5xx
        # 포함)를 forensics로 추적하기 위함. status_code로 성공/실패를 구분한다. DB I/O는
        # 동기라 이벤트 루프를 막지 않도록 threadpool로 오프로드한다.
        if request.method in _AUDIT_METHODS:
            await run_in_threadpool(
                _record_audit, req_id, user_id, request.method, safe_path,
                response.status_code, int(latency_ms),
            )
        # 성능 메트릭(RPA-103): 모든 요청(GET 포함)을 정규화된 경로로 request_metrics에.
        # 감사와 달리 응답을 기다리게 하지 않는다 — BackgroundTask는 응답 전송 **후** 실행되므로
        # 5ms짜리 조회에 관측 DB(Neon) 왕복 수백ms가 얹히지 않는다. (라우트가 background를 쓰는
        # 곳은 현재 없음 — 생기면 체이닝 필요)
        if request.method not in _METRIC_SKIP_METHODS:
            response.background = BackgroundTask(
                _record_metric, req_id, user_id, request.method, _normalize_path(safe_path),
                response.status_code, int(latency_ms),
            )
        return response
