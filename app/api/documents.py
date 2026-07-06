"""문서 업로드·조회 API (FR-01~04).

업로드 흐름: 검증 → 세션 확보 → 저장 → 파싱 → parsed_content 저장.
파싱 실패는 500이 아니라 문서 상태(failed)로 기록한다 — 사용자는 다른 파일로 재시도하면 된다.
"""

import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models
from app.db import get_db
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
