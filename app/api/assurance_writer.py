"""Protected machine-to-machine writer for Change Assurance records."""

from __future__ import annotations

import os
import re
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db import get_db
from app.services.assurance_evidence import persist_change_receipt
from assurance.change.foundation import AssuranceError


router = APIRouter(prefix="/api/internal/assurance", tags=["assurance-writer"])


class ChangePublisherSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=3, max_length=200)
    workflow_name: Literal["Change Assurance (Observe)"]
    workflow_run_id: int = Field(ge=1)
    run_attempt: int = Field(ge=1)
    event: Literal["pull_request"]
    conclusion: Literal["success"]
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    pull_request_number: int = Field(ge=1)


class ChangeReceiptEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    source: ChangePublisherSource
    artifacts: dict[str, dict[str, Any]]


def require_assurance_writer(request: Request) -> str:
    """Accept only the dedicated writer token; admin and Ops credentials are not equivalent."""
    expected = os.getenv("ASSURANCE_WRITER_TOKEN", "").strip()
    expected_repository = os.getenv("ASSURANCE_WRITER_REPOSITORY", "").strip()
    repository_pattern = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    if len(expected) < 32 or re.fullmatch(repository_pattern, expected_repository) is None:
        raise HTTPException(
            503,
            detail={
                "code": "ASSURANCE_WRITER_NOT_CONFIGURED",
                "message": "Change 판정 기록 writer가 구성되지 않았습니다.",
            },
        )
    authorization = request.headers.get("Authorization", "")
    scheme, separator, provided = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not provided
        or not secrets.compare_digest(provided, expected)
    ):
        raise HTTPException(
            401,
            detail={"code": "INVALID_ASSURANCE_WRITER", "message": "writer 인증에 실패했습니다."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return expected_repository


@router.post("/change-receipts", status_code=201)
def write_change_receipt(
    payload: ChangeReceiptEnvelope,
    expected_repository: str = Depends(require_assurance_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Validate a trusted workflow envelope again and append one Change receipt."""
    try:
        envelope = payload.model_dump(mode="python")
        if envelope["source"]["repository"] != expected_repository:
            raise AssuranceError("publisher repository is not authorized for this writer")
        result = persist_change_receipt(envelope, db)
    except AssuranceError as exc:
        raise AppError(
            "INVALID_ASSURANCE_EVIDENCE",
            "Change 판정 증거가 저장 계약을 충족하지 않습니다.",
            422,
        ) from exc
    except IntegrityError as exc:
        raise AppError(
            "ASSURANCE_RECEIPT_CONFLICT",
            "동일 식별자의 판정 기록이 기존 내용과 충돌합니다.",
            409,
        ) from exc
    return result
