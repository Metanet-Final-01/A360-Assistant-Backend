"""횡단 관심사(AOP) 미들웨어 — 모든 HTTP 요청을 라우트 코드 손 안 대고 관측한다.

한 곳에서:
- request-id 발급 → 요청 안의 파이프라인 로그(embed_query 등)가 같은 id로 묶이고 응답
  헤더(X-Request-ID)로도 나간다.
- 요청/응답 구조화 로그(method·path·status·latency·user) — app/rag/observability로.
- 성능 측정(latency).
- 감사 로그(누가·무엇을) → 변경성 요청(POST/PUT/PATCH/DELETE)의 성공만 audit_logs DB에.
  조회(GET)는 로그로만 남긴다 (중요 이벤트만 DB).

user_id는 Authorization 헤더의 JWT를 디코드해 얻는다 (DB 조회 없이, best-effort) —
의존성 실행 순서/스레드풀 전파에 기대지 않아 견고하다.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

# /debug, /api/rag/logs/recent는 디버그 콘솔이 1.5초마다 스스로 폴링하는 경로라 제외한다
# (그러지 않으면 로그가 자기 자신의 폴링 기록으로 도배된다).
_SKIP_HTTP_LOG_PREFIXES = ("/api/rag/logs/recent", "/debug")
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


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
        from app.db import SessionLocal

        uid = None
        if user_id:
            try:
                uid = _uuid.UUID(user_id)
            except (ValueError, TypeError):
                uid = None
        with SessionLocal() as s:
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
        common = {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "client": request.client.host if request.client else None,
            "user_id": user_id,
            "request_id": req_id,
            "started_at": started_at.isoformat(),
        }
        try:
            response = await call_next(request)
        except Exception as exc:
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

        # 감사: 변경성 요청의 성공만 DB에 (중요 이벤트만)
        if request.method in _AUDIT_METHODS and 200 <= response.status_code < 400:
            _record_audit(req_id, user_id, request.method, request.url.path,
                          response.status_code, int(latency_ms))
        return response
