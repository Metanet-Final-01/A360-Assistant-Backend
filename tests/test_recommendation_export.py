"""추천안 내보내기 엔드포인트 테스트 (RPA-79, FR-17)."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.db import get_db
from app.main import app

SID = uuid.uuid4()


def _rec() -> dict:
    return {
        "schema_version": "1.0",
        "steps": [{"step_id": "step-1", "actions": [
            {"order": 1, "package": "Browser", "action": "openbrowser", "label": "열기",
             "parameters": [], "children": []}]}],
        "variables": [], "notes": "",
    }


class FakeDB:
    def __init__(self, session=None, row=None):
        self.session = session
        self.row = row

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(db, user=None):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user


def test_export_returns_download_envelope():
    session = SimpleNamespace(id=SID, user_id=None)
    row = SimpleNamespace(id=uuid.uuid4(), version=3, source="drag", payload=_rec())
    _override(FakeDB(session=session, row=row))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/3/export")

    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and "v3.json" in cd  # 다운로드 헤더
    body = r.json()
    assert body["recommendation_version"] == 3 and body["source"] == "drag"
    assert body["recommendation"]["steps"][0]["step_id"] == "step-1"  # 트리 그대로
    assert "exported_at" in body


def test_export_404_missing_version():
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, row=None))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/99/export")
    assert r.status_code == 404


def test_export_blocks_non_owner():
    session = SimpleNamespace(id=SID, user_id=uuid.uuid4())
    _override(FakeDB(session=session, row=None), user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/1/export")
    assert r.status_code == 403


def test_export_non_int_version_422():
    """version은 int — /recommendations/latest 등과 라우트가 안 섞인다."""
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, row=None))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/notanint/export")
    assert r.status_code == 422


# --- docx 서식 문서 내보내기 (RPA-296, FR-17) ---

def _rec_rich() -> dict:
    """docx 렌더의 주요 섹션(개요·요구사항·입출력·trigger·변수·중첩 액션·질문카드·전제)을 모두 밟는다."""
    return {
        "schema_version": "1.0",
        "flow_confidence": 0.82,
        "steps": [{
            "step_id": "step-1", "label": "엑셀 열기", "description": "대상 파일을 연다",
            "actions": [{
                "order": 1, "package": "Excel_MS", "action": "OpenWorkbook", "label": "통합문서 열기",
                "parameters": [{"name": "path", "label": "파일 경로", "value": "C:/data.xlsx"}],
                "rationale": "업무정의서의 '엑셀 파일' 근거", "confidence": 0.9,
                "children": [{
                    "order": 2, "package": "Excel_MS", "action": "GoToCell", "label": "셀 이동",
                    "parameters": [{"name": "cellOption", "value": "A1"}], "children": [],
                }],
            }],
        }],
        "variables": [{"name": "vFile", "type": "STRING", "direction": "input", "description": "입력 파일"}],
        "trigger": {"kind": "schedule", "title": "매일 오전 9시", "reason": "'매일 아침' 표현",
                    "setup_hint": "Control Room 예약"},
        "needs_input": [{"card_id": "c1", "kind": "missing_param", "question": "파일 경로를 알려주세요",
                         "why": "열 파일이 필요", "blocking": True, "default": None}],
        "spec": {"goal": "매일 엑셀 집계 자동화",
                 "requirements": [{"req_id": "req-1", "text": "엑셀 열기", "priority": "must", "source": "doc"}],
                 "inputs": ["엑셀 파일"], "outputs": ["집계 결과"],
                 "assumptions": ["Knox 메일은 Email 패키지 기준"]},
        "notes": "테스트 주의사항",
    }


def test_export_docx_returns_formatted_document():
    """format=docx → 실제로 열리는 .docx이고, 카탈로그 표기·중첩 액션·주요 섹션이 담긴다."""
    from io import BytesIO

    from docx import Document

    session = SimpleNamespace(id=SID, user_id=None)
    row = SimpleNamespace(id=uuid.uuid4(), version=5, source="agent", payload=_rec_rich())
    _override(FakeDB(session=session, row=row))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/5/export", params={"format": "docx"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and "v5.docx" in cd
    assert len(r.content) > 0

    doc = Document(BytesIO(r.content))
    blob = "\n".join(p.text for p in doc.paragraphs)
    blob += "\n" + "\n".join(cell.text for t in doc.tables for row_ in t.rows for cell in row_.cells)
    assert "A360 작업 추천안" in blob
    assert "Excel_MS / OpenWorkbook" in blob     # 카탈로그 표기(package/action)
    assert "Excel_MS / GoToCell" in blob          # 컨테이너 children도 재귀 렌더
    assert "매일 오전 9시" in blob                 # trigger
    assert "vFile" in blob                         # 변수표
    assert "매일 엑셀 집계 자동화" in blob          # 개요(spec.goal)
    assert "파일 경로를 알려주세요" in blob          # 질문카드
    assert "Knox 메일은 Email 패키지 기준" in blob   # 전제(assumptions)


def test_export_json_is_default_and_unchanged():
    """format 미지정·명시 json 모두 기존 JSON 봉투 — 하위호환."""
    session = SimpleNamespace(id=SID, user_id=None)
    row = SimpleNamespace(id=uuid.uuid4(), version=3, source="drag", payload=_rec())
    _override(FakeDB(session=session, row=row))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/3/export", params={"format": "json"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["recommendation"]["steps"][0]["step_id"] == "step-1"


def test_export_invalid_format_422():
    """format은 json|docx만 — 그 외는 422(pdf 등 미지원 포맷 오인 방지)."""
    session = SimpleNamespace(id=SID, user_id=None)
    row = SimpleNamespace(id=uuid.uuid4(), version=1, source="drag", payload=_rec())
    _override(FakeDB(session=session, row=row))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/1/export", params={"format": "pdf"})
    assert r.status_code == 422
