"""문서 업로드·조회 API (FR-01~04).

업로드 흐름: 검증 → 세션 확보 → 저장 → 파싱 → parsed_content 저장.
파싱 실패는 500이 아니라 문서 상태(failed)로 기록한다 — 사용자는 다른 파일로 재시도하면 된다.
"""

import contextvars
import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.api.auth import assert_session_owner, get_optional_user
from app.core.llm import usage_context
from app.db import get_db
from app.schemas import ProgressEvent
from app.services import storage
from app.services.parser import parse_document, parse_text
from app.services.upload_security import validate_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["documents"])


def _document_out(doc: models.Document) -> dict:
    parsed = doc.parsed_content or {}
    return {
        "id": str(doc.id),
        "session_id": str(doc.session_id),
        "filename": doc.filename,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "error": doc.error,
        "page_count": parsed.get("page_count"),
        "warnings": parsed.get("warnings", []),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _get_document_or_404(
    document_id: str, db: Session, user: models.User | None = None
) -> models.Document:
    try:
        key = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(
            400, detail={"code": "INVALID_ID", "message": "잘못된 문서 ID 형식입니다."}
        ) from None
    doc = db.get(models.Document, key)
    if doc is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "문서를 찾을 수 없습니다."})
    # 문서가 속한 세션의 소유권 검사 (남의 세션 문서 조회 차단).
    # Document.session_id는 NOT NULL이고 세션 삭제 시 문서도 CASCADE 삭제되므로 정상
    # 상태에선 세션이 항상 존재한다. 혹시 없으면(DB 불일치) 소유권을 확인할 수 없으니
    # fail-closed로 404 처리한다 — 검사 없이 반환하지 않는다.
    session = db.get(models.AnalysisSession, doc.session_id)
    if session is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "문서를 찾을 수 없습니다."})
    assert_session_owner(session, user)
    return doc


@router.post("/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """업무정의서 업로드 → 검증·저장만 하고 즉시 반환한다 (status="uploaded").

    파싱은 시간이 걸릴 수 있어 분리했다: 업로드 응답을 빠르게 돌려주고, 클라이언트는
    이어서 POST /documents/{id}/parse (SSE)로 파싱 진행을 소비한다. 파싱 완료 전까지
    page_count/warnings는 비어 있다.
    """
    max_mb = int(os.getenv("MAX_UPLOAD_MB", "20"))
    content = await file.read(max_mb * 1024 * 1024 + 1)
    filename = file.filename or "unnamed"
    content_type = validate_upload(filename, content, max_mb)  # 실패 시 400/413

    session = _resolve_session(session_id, filename, db, user)

    doc = models.Document(
        session_id=session.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        status="uploaded",
    )
    db.add(doc)
    db.flush()

    doc.storage_path = storage.save(str(session.id), str(doc.id), filename, content)
    db.commit()

    return _document_out(doc)


def _resolve_session(
    session_id: str | None, title: str, db: Session, user: models.User | None
) -> models.AnalysisSession:
    """기존 세션(소유권 검사) 또는 신규 세션(소유자 기록)을 확보한다."""
    if session_id:
        try:
            session = db.get(models.AnalysisSession, uuid.UUID(session_id))
        except ValueError:
            session = None
        if session is None:
            raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다."})
        assert_session_owner(session, user)  # 남의 세션에 추가 차단
        return session
    # 로그인 사용자면 세션 소유자로 기록 — 이후 접근 시 소유권 검사의 기준
    session = models.AnalysisSession(title=title, user_id=user.id if user else None)
    db.add(session)
    db.flush()
    return session


@router.post("/documents/{document_id}/parse")
def parse_document_stream(
    document_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """업로드된 문서를 파싱한다 (FR-02, 04) — SSE 스트림.

    이벤트: stage(parsing) → done(data=문서 메타) / error. 파싱 실패는 status=failed로
    기록하고 error 이벤트를 보낸다 (업로드 자체는 성공이므로 문서는 남는다).
    """
    doc = _get_document_or_404(document_id, db, user)
    if doc.status == "parsed":
        # 이미 파싱됨 — 재파싱하지 않고 현재 결과를 done으로 즉시 반환
        summary = _document_out(doc)

        def _already():
            yield ProgressEvent(event="done", stage="parsing", message="이미 파싱된 문서입니다", data=summary).to_sse()

        return StreamingResponse(_already(), media_type="text/event-stream")

    doc_id, filename, storage_path = doc.id, doc.filename, doc.storage_path

    def sse():
        try:
            yield ProgressEvent(event="stage", stage="parsing", message="문서를 분석용으로 파싱하고 있습니다").to_sse()
            content = storage.load(storage_path)
            parsed = parse_document(filename, content)

            from app.db import SessionLocal

            with SessionLocal() as s:
                fresh = s.get(models.Document, doc_id)
                fresh.parsed_content = parsed
                fresh.status = "parsed"
                s.commit()
                summary = _document_out(fresh)
            yield ProgressEvent(event="done", stage="parsing", message="파싱 완료", data=summary).to_sse()
        except Exception as e:  # noqa: BLE001 — 파싱 실패는 문서 상태로 기록
            logger.exception("문서 파싱 실패: document=%s", doc_id)
            _mark_parse_failed(doc_id, str(e))
            yield ProgressEvent(
                event="error", stage="parsing", message="문서 파싱에 실패했습니다 (다른 파일로 다시 시도해 주세요)"
            ).to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _mark_parse_failed(document_id: uuid.UUID, error: str) -> None:
    try:
        from app.db import SessionLocal

        with SessionLocal() as s:
            fresh = s.get(models.Document, document_id)
            if fresh is not None:
                fresh.status = "failed"
                fresh.error = f"파싱 실패: {error}"
                s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("파싱 실패 기록마저 실패: document=%s", document_id)


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000, description="자연어 업무 요청")
    session_id: str | None = None


@router.post("/documents/text", status_code=201)
def create_document_from_text(
    payload: TextRequest,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """자연어 업무 요청을 문서처럼 등록한다 (파일 없이 텍스트로 분석 시작).

    파싱이 필요 없으므로 status="parsed"로 즉시 반환 → 곧바로 analyze 가능.
    """
    title = payload.text.strip().splitlines()[0][:80] if payload.text.strip() else "자연어 요청"
    session = _resolve_session(payload.session_id, title, db, user)

    doc = models.Document(
        session_id=session.id,
        filename=f"{title}.txt",
        content_type="text/plain",
        size_bytes=len(payload.text.encode("utf-8")),
        status="parsed",
        parsed_content=parse_text(payload.text),
    )
    db.add(doc)
    db.commit()
    return _document_out(doc)


@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """문서 메타데이터·처리 상태 조회."""
    return _document_out(_get_document_or_404(document_id, db, user))


@router.get("/documents/{document_id}/content")
def get_document_content(
    document_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """파싱 결과(구조화 JSON) 조회 — 분석(FR-05) 입력으로 사용된다."""
    doc = _get_document_or_404(document_id, db, user)
    if doc.status != "parsed" or doc.parsed_content is None:
        raise HTTPException(
            409,
            detail={"code": "NOT_PARSED", "message": f"파싱이 완료되지 않았습니다 (현재 상태: {doc.status})."},
        )
    return {"id": str(doc.id), "parsed_content": doc.parsed_content}


@router.post("/documents/{document_id}/enrich-vision")
def enrich_vision(
    document_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """텍스트가 부족한 페이지를 비전 LLM으로 보강한다 (FR-03) — SSE 스트림.

    페이지당 LLM 호출로 수십 초가 걸릴 수 있어 진행 상황을 ProgressEvent로 흘린다
    (규약: docs/INTERFACES.md §5). 프론트는 EventSource가 아닌 fetch 스트리밍으로
    소비한다 (POST라서). 텍스트 임계값 미만 페이지만 선별하므로 보강할 페이지가
    없으면 LLM 비용 없이 즉시 done이 온다.
    """
    doc = _get_document_or_404(document_id, db, user)
    if doc.status != "parsed" or doc.parsed_content is None:
        raise HTTPException(
            409,
            detail={"code": "NOT_PARSED", "message": f"파싱이 완료되지 않았습니다 (현재 상태: {doc.status})."},
        )

    from app.services.parser import vision

    file_content = storage.load(doc.storage_path)

    doc_id, filename, parsed_content, sess_id = doc.id, doc.filename, doc.parsed_content, doc.session_id
    user_id = user.id if user else None

    # 비전 LLM 사용을 component=vision·사용자로 귀속. usage_context를 제너레이터의 yield
    # 너머로 걸치면 안 된다 — StreamingResponse가 next()마다 다른 스레드 컨텍스트에서
    # 재개해 ContextVar가 끊긴다. 대신 여기(요청 스레드)서 귀속이 설정된 컨텍스트를
    # 복사해 두고, 스트림의 매 재개를 그 컨텍스트 안에서 실행한다.
    with usage_context(component="vision", actor_type="user", user_id=user_id, session_id=sess_id):
        stream_ctx = contextvars.copy_context()

    def sse():
        inner = _run_vision_stream(filename, file_content, parsed_content, sess_id, doc_id)
        try:
            while True:
                try:
                    event = stream_ctx.run(next, inner)
                except StopIteration:
                    break
                yield event
        except RuntimeError as e:  # OPENAI_API_KEY 미설정 등
            yield ProgressEvent(event="error", stage="vision", message=f"LLM 구성 오류: {e}").to_sse()
        except Exception:  # noqa: BLE001
            logger.exception("비전 보강 실패: document=%s", doc_id)
            yield ProgressEvent(
                event="error", stage="vision", message="비전 보강 중 오류가 발생했습니다"
            ).to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _run_vision_stream(filename, file_content, parsed_content, sess_id, doc_id):
    """enrich_vision의 SSE 본문 — usage_context 블록 안에서 실행된다."""
    from app.services.parser import vision

    for event in vision.enrich_document_stream(
        filename, file_content, parsed_content, session_id=sess_id
    ):
        if event.event == "done":
            # 요청 스코프의 db 세션은 스트리밍 시작 전에 닫히므로(FastAPI 0.106+
            # yield 의존성 동작) 저장은 반드시 새 세션으로 한다
            from app.db import SessionLocal

            with SessionLocal() as s:
                fresh = s.get(models.Document, doc_id)
                fresh.parsed_content = event.data["parsed"]
                s.commit()
                summary = _document_out(fresh)
            yield ProgressEvent(
                event="done",
                stage="vision",
                message=event.message,
                data={**summary, "enriched_pages": event.data["enriched_pages"]},
            ).to_sse()
        else:
            yield event.to_sse()
