"""문서 업로드·조회 API (FR-01~04).

업로드 흐름: 검증 → 세션 확보 → 저장 → 파싱 → parsed_content 저장.
파싱 실패는 500이 아니라 문서 상태(failed)로 기록한다 — 사용자는 다른 파일로 재시도하면 된다.
"""

import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import models
from app.api.auth import get_optional_user
from app.core.llm import usage_context
from app.db import get_db
from app.schemas import ProgressEvent
from app.services import storage
from app.services.parser import parse_document
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


def _get_document_or_404(document_id: str, db: Session) -> models.Document:
    try:
        key = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(400, detail={"code": "INVALID_ID", "message": "잘못된 문서 ID 형식입니다."})
    doc = db.get(models.Document, key)
    if doc is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "문서를 찾을 수 없습니다."})
    return doc


@router.post("/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    db: Session = Depends(get_db),
) -> dict:
    """업무정의서 업로드 → 검증·저장·파싱까지 한 번에 수행한다."""
    max_mb = int(os.getenv("MAX_UPLOAD_MB", "20"))
    content = await file.read(max_mb * 1024 * 1024 + 1)
    filename = file.filename or "unnamed"
    content_type = validate_upload(filename, content, max_mb)  # 실패 시 400/413

    if session_id:
        try:
            session = db.get(models.AnalysisSession, uuid.UUID(session_id))
        except ValueError:
            session = None
        if session is None:
            raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다."})
    else:
        session = models.AnalysisSession(title=filename)
        db.add(session)
        db.flush()

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
    doc.status = "parsing"
    db.commit()

    try:
        doc.parsed_content = parse_document(filename, content)
        doc.status = "parsed"
    except Exception as e:  # noqa: BLE001 — 파싱 실패는 문서 상태로 기록
        logger.exception("문서 파싱 실패: %s", filename)
        doc.status = "failed"
        doc.error = f"파싱 실패: {e}"
    db.commit()

    return _document_out(doc)


@router.get("/documents/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db)) -> dict:
    """문서 메타데이터·처리 상태 조회."""
    return _document_out(_get_document_or_404(document_id, db))


@router.get("/documents/{document_id}/content")
def get_document_content(document_id: str, db: Session = Depends(get_db)) -> dict:
    """파싱 결과(구조화 JSON) 조회 — 분석(FR-05) 입력으로 사용된다."""
    doc = _get_document_or_404(document_id, db)
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
    doc = _get_document_or_404(document_id, db)
    if doc.status != "parsed" or doc.parsed_content is None:
        raise HTTPException(
            409,
            detail={"code": "NOT_PARSED", "message": f"파싱이 완료되지 않았습니다 (현재 상태: {doc.status})."},
        )

    from app.services.parser import vision

    file_content = storage.load(doc.storage_path)

    doc_id, filename, parsed_content, sess_id = doc.id, doc.filename, doc.parsed_content, doc.session_id
    user_id = user.id if user else None

    def sse():
        # 비전 LLM 사용을 component=vision·사용자로 귀속. vision 내부의 ThreadPoolExecutor는
        # copy_context로 이 컨텍스트를 워커까지 전파한다.
        try:
            with usage_context(
                component="vision", actor_type="user", user_id=user_id, session_id=sess_id
            ):
                yield from _run_vision_stream(filename, file_content, parsed_content, sess_id, doc_id)
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
