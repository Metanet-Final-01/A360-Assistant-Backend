"""횡단 관심사(AOP) 테스트 (RPA-49) — 관측성 미들웨어 + 전역 에러 핸들러 + 감사 로그."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import app.core.http_logging as httplog
from app.core.errors import AppError, install_error_handlers
from app.core.http_logging import register_http_logging


@pytest.fixture()
def mini_app(monkeypatch):
    """미들웨어·에러 핸들러만 얹은 최소 앱 (라우트를 자유롭게 정의)."""
    application = FastAPI()
    register_http_logging(application)
    install_error_handlers(application)
    return application


# --- 전역 에러 핸들러 (공통 에러) ---

def test_app_error_maps_to_code_message(mini_app):
    @mini_app.get("/boom-app")
    def _boom():
        raise AppError("MY_CODE", "사용자용 메시지", status_code=418)

    with TestClient(mini_app) as c:
        r = c.get("/boom-app")
    assert r.status_code == 418
    assert r.json()["detail"] == {"code": "MY_CODE", "message": "사용자용 메시지"}


def test_unexpected_exception_becomes_500_envelope(mini_app):
    @mini_app.get("/boom-raw")
    def _boom():
        raise ValueError("내부 폭발 — 클라이언트에 새면 안 됨")

    # TestClient가 서버 예외를 재발생시키지 않도록 (핸들러가 처리하는지 보려고)
    with TestClient(mini_app, raise_server_exceptions=False) as c:
        r = c.get("/boom-raw")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"]["code"] == "INTERNAL_ERROR"
    assert "폭발" not in body["detail"]["message"]  # 원본 메시지·스택 미노출


# --- 미들웨어: request-id 헤더 + SSE 안 깨짐 ---

def test_request_id_header_present(mini_app):
    @mini_app.get("/ok")
    def _ok():
        return {"ok": True}

    with TestClient(mini_app) as c:
        r = c.get("/ok")
    assert r.headers.get("X-Request-ID")


def test_streaming_response_not_buffered(mini_app):
    """미들웨어가 SSE(스트리밍)를 버퍼링·중단시키지 않아야 한다."""

    @mini_app.get("/stream")
    def _stream():
        def gen():
            for i in range(3):
                yield f"data: chunk{i}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    with TestClient(mini_app) as c:
        with c.stream("GET", "/stream") as r:
            chunks = [ln for ln in r.iter_lines() if ln.startswith("data:")]
    assert chunks == ["data: chunk0", "data: chunk1", "data: chunk2"]


# --- 감사 로그 DB 기록 ---

def test_audit_records_mutating_success_only(mini_app, monkeypatch):
    saved = []

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): saved.append(row)
        def commit(self): pass

    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr("app.models.AuditLog", lambda **kw: SimpleNamespace(**kw))

    @mini_app.post("/do")
    def _do():
        return {"done": True}

    @mini_app.get("/read")
    def _read():
        return {"data": 1}

    with TestClient(mini_app) as c:
        c.post("/do")     # 변경성 성공 → 감사 기록됨
        c.get("/read")    # 조회 → 감사 안 됨

    assert len(saved) == 1
    assert saved[0].method == "POST" and saved[0].path == "/do" and saved[0].status_code == 200


def test_audit_records_failed_mutation(mini_app, monkeypatch):
    """실패한 변경 요청(4xx)도 감사에 남긴다 — "누가 무엇을 시도했나" forensics (RPA-82)."""
    saved = []

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): saved.append(row)
        def commit(self): pass

    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr("app.models.AuditLog", lambda **kw: SimpleNamespace(**kw))

    @mini_app.post("/fail")
    def _fail():
        raise AppError("NOPE", "실패", status_code=400)  # 전역 핸들러가 400 응답으로 변환

    with TestClient(mini_app) as c:
        c.post("/fail")  # 4xx 변경 요청 → 감사에 기록됨(status_code=400)

    assert len(saved) == 1
    assert saved[0].method == "POST" and saved[0].path == "/fail" and saved[0].status_code == 400


def test_no_crlf_sanitizer():
    """로그 인젝션 방지 — 경로/쿼리의 CR/LF 제거 (원시 ASGI 서버 대비 defense-in-depth).

    httpx/Starlette가 전송 단계에서 CRLF를 이미 벗기지만, 미들웨어는 request.url.path를
    로그·감사에 넣기 전 한 번 더 방어한다."""
    from app.core.http_logging import _no_crlf

    assert _no_crlf("/api/x\r\nFAKE-LOG 200 OK") == "/api/xFAKE-LOG 200 OK"
    assert _no_crlf("q=1\nq=2") == "q=1q=2"
    assert _no_crlf("/normal/path") == "/normal/path"


def test_audit_captures_user_from_jwt(mini_app, monkeypatch):
    saved = []

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): saved.append(row)
        def commit(self): pass

    uid = uuid.uuid4()
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr("app.models.AuditLog", lambda **kw: SimpleNamespace(**kw))
    # JWT 디코드를 목킹 — 미들웨어가 Authorization에서 user_id를 뽑는지
    monkeypatch.setattr("app.core.security.decode_access_token", lambda tok: str(uid))

    @mini_app.post("/act")
    def _act():
        return {"ok": True}

    with TestClient(mini_app) as c:
        c.post("/act", headers={"Authorization": "Bearer faketoken"})

    assert len(saved) == 1
    assert saved[0].user_id == uid
