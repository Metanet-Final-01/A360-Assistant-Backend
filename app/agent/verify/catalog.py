"""카탈로그 조회 인터페이스 — (package, action) → 구조 스펙.

구조 스펙은 백엔드가 JAR `package.json`에서 정규화해 `rag_documents.metadata.schema`에
저장해 둔 것과 같은 형태다(jar_parser 산출: name/label/type/required/options/default).
검수 하네스(checker)는 RAG 청크가 아니라 이 구조 스펙으로 검증한다 — 렌더 문자열은
파라미터 기계명·enum이 손실돼 있어 골드셋 표기 검사에 못 쓴다.

에이전트는 DB에 직접 접근하지 않으므로(INTERFACES 소유권), 실제 조회는 백엔드가
`get_action_schema(package, action)` 서비스로 제공하는 것을 목표로 한다 —
`search_actions`(RPA-9)와 같은 경계. 그 서비스가 붙기 전까지는 FakeCatalog(개발용
축약 픽스처)를 쓴다. retrieval.py의 FakeRetriever→HybridRetriever 교체와 같은 패턴:
이 파일의 get_catalog()만 바꾸면 나머지는 손대지 않는다.

스펙 dict 형태:
    {
      "package": "Excel_MS", "action": "GoToCell", "label": "셀로 이동",
      "return_type": None,
      "parameters": [
        {"name": "cellOption", "label": "셀 옵션", "type": "RADIO", "required": True,
         "options": [{"label": "특정 셀", "value": "specific"}, ...]},
        {"name": "session", "label": "세션 이름", "type": "SESSION", "required": True},
      ],
    }
"""

import os
from typing import Protocol


class CatalogLookup(Protocol):
    """구조 스펙 조회 계약. 없는 액션이면 None."""

    def get_action_schema(self, package: str, action: str) -> dict | None: ...


_MISSING = object()


def _p(name, type_, required=False, options=None, label="", default=_MISSING):
    """파라미터 스펙 한 건을 만드는 헬퍼 (픽스처 가독성용)."""
    param: dict = {"name": name, "label": label or name, "type": type_, "required": required}
    if options is not None:
        param["options"] = [{"label": o, "value": o} for o in options]
    if default is not _MISSING:
        param["default"] = default
    return param


# 개발·테스트용 축약 카탈로그. 실제 KB(357개 액션 스키마)가 백엔드 조회로 붙기 전까지의
# 최소 데이터로, 골드 흐름(a360-gold-flow)과 검수 규칙(R1~R6)을 돌리기에 충분한
# 대표 액션만 담는다. package/action/파라미터 name은 RAG_CATALOG.md 표기를 따른다.
_FAKE_ACTIONS: list[dict] = [
    # --- WebAutomation (웹 조작) ---
    {"package": "WebAutomation", "action": "StartSessionWebAutomation", "label": "Start Session",
     "parameters": [_p("browserType", "SELECT", True, ["Chrome", "Edge"], "브라우저"),
                    _p("sessionName", "TEXT", True, label="세션 이름")]},
    {"package": "WebAutomation", "action": "openpage", "label": "Open Page",
     "parameters": [_p("url", "TEXT", True, label="URL"),
                    _p("sessionName", "TEXT", True, label="세션 이름")]},
    {"package": "WebAutomation", "action": "pageloaded", "label": "Wait Page Loaded",
     "parameters": [_p("sessionName", "TEXT", True, label="세션 이름")]},
    {"package": "WebAutomation", "action": "clickelement", "label": "Click",
     "parameters": [_p("element", "UIOBJECT", True, label="대상 요소"),
                    _p("sessionName", "TEXT", True, label="세션 이름")]},
    {"package": "WebAutomation", "action": "gettablecontent", "label": "Get Table Content",
     "parameters": [_p("element", "UIOBJECT", True, label="테이블 요소"),
                    _p("sessionName", "TEXT", True, label="세션 이름")]},
    {"package": "WebAutomation", "action": "EndSessionWebAutomation", "label": "End Session",
     "parameters": [_p("sessionName", "TEXT", True, label="세션 이름")]},
    # --- Excel_MS (엑셀 고급) ---
    {"package": "Excel_MS", "action": "OpenSpreadsheet", "label": "열기",
     "parameters": [_p("filePath", "FILE", True, label="파일 경로"),
                    _p("openMode", "RADIO", True, ["읽기 전용 모드", "쓰기 전용 모드"], "열기"),
                    _p("session", "TEXT", True, label="세션 이름", default="Default")]},
    {"package": "Excel_MS", "action": "CreateSpreadsheet", "label": "통합 문서 생성",
     "parameters": [_p("filePath", "FILE", True, label="파일 경로"),
                    _p("session", "TEXT", True, label="세션 이름", default="Default")]},
    {"package": "Excel_MS", "action": "GoToCell", "label": "셀로 이동",
     "parameters": [_p("cellOption", "RADIO", True, ["특정 셀", "활성 셀"], "셀 옵션"),
                    _p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "SetCell", "label": "셀 설정",
     "parameters": [_p("cellAddress", "TEXT", True, label="셀 주소"),
                    _p("cellValue", "TEXT", False, label="값"),
                    _p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "writeDataTableToWorksheet", "label": "데이터 테이블로부터 쓰기",
     "parameters": [_p("dataTable", "VARIABLE", True, label="데이터 테이블"),
                    _p("cellAddress", "TEXT", True, label="시작 셀"),
                    _p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "InsertDeleteRowColumn", "label": "행/열 삽입·삭제",
     "parameters": [_p("rowOperationsRequested", "RADIO", True, ["행 작업", "열 작업"], "작업"),
                    _p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "SelectRowColumnCellRange", "label": "셀/행/열 선택",
     "parameters": [_p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "RunMacro", "label": "매크로 실행",
     "parameters": [_p("macroName", "TEXT", True, label="매크로 이름"),
                    _p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "SaveSpreadSheet", "label": "통합 문서 저장",
     "parameters": [_p("session", "SESSION", True, label="세션 이름")]},
    {"package": "Excel_MS", "action": "CloseSpreadsheet", "label": "닫기",
     "parameters": [_p("session", "SESSION", True, label="세션 이름")]},
    # --- Email (이메일) ---
    {"package": "Email", "action": "emailConnect", "label": "연결",
     "parameters": [_p("host", "TEXT", True, label="서버"),
                    _p("port", "NUMBER", True, label="포트")]},
    {"package": "Email", "action": "sendMail", "label": "보내기",
     "parameters": [_p("to", "TEXT", True, label="To 주소"),
                    _p("subject", "TEXT", True, label="제목"),
                    _p("attachment", "FILE", False, label="첨부"),
                    _p("sendVia", "SELECT", True, ["이메일 서버", "Outlook", "EWS 서버"], "전송 경로"),
                    _p("message", "TEXTAREA", True, label="메시지")]},
    {"package": "Email", "action": "closeEmail", "label": "연결 끊기", "parameters": []},
    # --- 흐름 제어 (컨테이너) ---
    {"package": "Loop", "action": "loop.commands.start", "label": "루프",
     "parameters": [_p("iteratorType", "SELECT", True,
                       ["For each row in a data table", "N times", "For each item in list"], "반복 유형")]},
    {"package": "If", "action": "if", "label": "If",
     "parameters": [_p("condition", "CONDITION", True, label="조건")]},
    {"package": "If", "action": "elseIf", "label": "Else If",
     "parameters": [_p("condition", "CONDITION", True, label="조건")]},
    {"package": "If", "action": "else", "label": "Else", "parameters": []},
    {"package": "Step", "action": "step", "label": "단계",
     "parameters": [_p("title", "TEXT", False, label="제목")]},
    {"package": "ErrorHandler", "action": "try", "label": "Try", "parameters": []},
    {"package": "ErrorHandler", "action": "catch", "label": "Catch",
     "parameters": [_p("exception", "EXCEPTION", True, label="예외"),
                    _p("continueOnError", "CHECKBOX", False, label="오류 발생 시 계속")]},
    {"package": "ErrorHandler", "action": "finally", "label": "Finally", "parameters": []},
    # --- 글루 ---
    {"package": "Datetime", "action": "toString", "label": "To 문자열",
     "parameters": [_p("datetime", "VARIABLE", True, label="날짜/시간"),
                    _p("format", "TEXT", False, label="형식")]},
    {"package": "String", "action": "assign", "label": "지정",
     "parameters": [_p("value", "TEXT", True, label="값")]},
]

_FAKE_INDEX: dict[tuple[str, str], dict] = {
    (a["package"], a["action"]): a for a in _FAKE_ACTIONS
}


class FakeCatalog:
    """개발·테스트용 조회기. 실제 백엔드 카탈로그 서비스가 붙기 전까지의 스텁."""

    def get_action_schema(self, package: str, action: str) -> dict | None:
        return _FAKE_INDEX.get((package, action))


_fake_catalog: CatalogLookup = FakeCatalog()
_catalog_cache: dict[str, CatalogLookup] = {}


def get_catalog() -> CatalogLookup:
    """checker·shortlist가 쓰는 카탈로그 조회기.

    AGENT_CATALOG=backend이면 백엔드 카탈로그 서비스(app.services.catalog)를,
    아니면 FakeCatalog(스텁)를 반환한다. 기본은 fake라 인프라 없이도 그래프가 돈다.
    백엔드 서비스가 준비되면 이 분기만 실제 구현으로 바꾼다.

    카탈로그는 정적 참조 데이터라 mode별로 1회만 해석해 캐싱한다 — 병렬 step 노드가
    매 호출마다 재조회(특히 backend 서비스 재생성)하지 않게 한다 (RPA-27 리뷰).
    """
    mode = "backend" if os.getenv("AGENT_CATALOG", "fake").lower() == "backend" else "fake"
    cached = _catalog_cache.get(mode)
    if cached is not None:
        return cached

    if mode == "backend":
        from app.services.catalog import get_backend_catalog

        catalog = get_backend_catalog()
    else:
        catalog = _fake_catalog
    _catalog_cache[mode] = catalog
    return catalog
