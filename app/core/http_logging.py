"""모든 HTTP 요청을 AOP 스타일로 기록하는 미들웨어 (각 라우트 코드는 손대지 않음).

app/rag/observability의 요청 단위 로깅과 연동한다 — 미들웨어가 request_id를
발급하면 그 요청 안의 파이프라인 로그(embed_query 등)가 같은 id로 묶인다.
"""

import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request

# /debug, /api/rag/logs/recent는 디버그 콘솔이 1.5초마다 스스로 폴링하는 경로라 제외한다
# (그러지 않으면 로그가 자기 자신의 폴링 기록으로 도배된다).
_SKIP_HTTP_LOG_PREFIXES = ("/api/rag/logs/recent", "/debug")


def register_http_logging(app: FastAPI) -> None:
    @app.middleware("http")
    async def log_http_requests(request: Request, call_next):
        from app.rag.observability import log_event, new_request_id

        if request.url.path.startswith(_SKIP_HTTP_LOG_PREFIXES):
            return await call_next(request)

        new_request_id()  # 이 HTTP 요청 안의 모든 파이프라인 로그(embed_query 등)를 이 id로 묶는다
        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()
        common = {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "client": request.client.host if request.client else None,
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

        log_event(
            "http_request",
            **common,
            status_code=response.status_code,
            status="ok" if response.status_code < 400 else "error",
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        return response
