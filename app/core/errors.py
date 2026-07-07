"""전역 에러 핸들링 (횡단 관심사 — 자바 @ControllerAdvice 대응).

라우트마다 try/except로 만들던 {code, message} 응답을 한 곳으로 모은다.
- AppError를 raise하면 지정한 code/status로 표준 응답.
- 미포착 예외는 500 INTERNAL_ERROR로 감싸고 request-id와 함께 로깅 — 스택트레이스가
  클라이언트로 새지 않는다.

FastAPI가 이미 처리하는 HTTPException(detail={code,message})은 그대로 둔다.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.rag.observability import get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    """도메인 에러 — 라우트/서비스가 raise하면 전역 핸들러가 {code,message}로 응답한다."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _payload(code: str, message: str) -> dict:
    body: dict = {"detail": {"code": code, "message": message}}
    rid = get_request_id()
    if rid:
        body["request_id"] = rid  # 클라이언트가 문의 시 이 id로 서버 로그를 찾는다
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_payload(exc.code, exc.message))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 미포착 예외 — request-id와 함께 스택을 서버 로그에만 남기고, 클라이언트엔 일반 메시지.
        # 경로의 CRLF는 제거한다 (외부 입력이 로그 라인을 위조하는 로그 인젝션 방지).
        safe_path = request.url.path.replace("\r", "").replace("\n", "")
        logger.exception("미포착 예외: %s %s (request_id=%s)", request.method, safe_path, get_request_id())
        return JSONResponse(
            status_code=500,
            content=_payload("INTERNAL_ERROR", "서버 내부 오류가 발생했습니다"),
        )
