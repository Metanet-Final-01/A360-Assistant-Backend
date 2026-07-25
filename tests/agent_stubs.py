"""테스트용 인메모리 검색기·카탈로그 스텁 (RPA-69).

프로덕션 경로에선 항상 실제 RAG(HybridRetriever/BackendCatalog)를 쓴다. 유닛 테스트는
인프라(pgvector·OpenSearch·DB) 없이 돌아야 하므로, conftest.py의 autouse fixture가
app/agent의 검색기·카탈로그 팩토리를 이 스텁으로 주입한다. 개별 테스트가 자기만의
페이크를 원하면 monkeypatch로 다시 덮으면 된다(예: test_recommend_graph).

FakeRetriever는 키워드 매칭, FakeCatalog는 대표 액션 ~30개 픽스처다 — 골드 흐름과
검수 규칙(R1~R6)을 돌리기에 충분한 최소 데이터. package/action/파라미터 name은
RAG_CATALOG.md 표기를 따른다.
"""


# ─────────────────────────────────────────────────────────────────────────────
# 검색기 스텁
# ─────────────────────────────────────────────────────────────────────────────

# keywords가 질문에 포함되면 매칭된다 (keywords는 검색 결과에서 제외).
_FAKE_DOCS: list[dict] = [
    {
        "id": "stub-excel-advanced-read",
        "source_type": "stub",
        "package_name": "Excel advanced",
        "action_name": "Open / Get multiple cells",
        "title": "Excel advanced — 통합 문서 열기·셀 범위 읽기",
        "url": None,
        "content": (
            "Excel advanced 패키지의 Open 액션으로 통합 문서를 연다(파일 경로, "
            "'Read-only' 여부, 시트 지정). Get multiple cells 액션으로 셀 범위를 "
            "데이터 테이블 변수에 읽어오고, 행 단위 처리는 Loop 액션의 "
            "'For each row in worksheet'와 함께 쓴다. 작업 후 Close 액션으로 닫는다."
        ),
        "keywords": ["엑셀", "excel", "xlsx", "스프레드시트", "시트", "워크시트", "읽"],
    },
    {
        "id": "stub-browser-recorder",
        "source_type": "stub",
        "package_name": "Recorder",
        "action_name": "Capture",
        "title": "Recorder — 웹 화면 캡처 기반 브라우저 자동화",
        "url": None,
        "content": (
            "Recorder 패키지의 Capture 액션으로 브라우저(Chrome, Edge) 화면의 버튼 "
            "클릭·텍스트 입력·값 추출을 자동화한다. 대상 창(Window)과 UI 요소를 "
            "지정하고, 로그인 폼 입력처럼 반복되는 웹 작업 단계를 구성한다. 페이지 "
            "이동·URL 열기는 Browser 패키지의 액션을 함께 쓴다."
        ),
        "keywords": ["브라우저", "browser", "웹", "크롬", "chrome", "클릭", "로그인", "recorder", "캡처"],
    },
    {
        "id": "stub-datatable-ops",
        "source_type": "stub",
        "package_name": "Data Table",
        "action_name": "Filter / Sort",
        "title": "Data Table — 표 데이터 필터·정렬·가공",
        "url": None,
        "content": (
            "Data Table 패키지로 데이터 테이블 변수를 가공한다. Filter 액션으로 조건에 "
            "맞는 행만 남기고, Sort 액션으로 특정 컬럼 기준 정렬한다. Excel에서 읽어온 "
            "데이터를 중간 가공할 때 주로 쓰며, 결과는 다시 Excel 기록이나 Loop 처리에 "
            "넘긴다."
        ),
        "keywords": ["데이터 테이블", "data table", "필터", "정렬", "가공", "집계"],
    },
    {
        "id": "stub-email-send",
        "source_type": "stub",
        "package_name": "Email",
        "action_name": "Send",
        "title": "Email — 메일 발송(첨부 포함)",
        "url": None,
        "content": (
            "Email 패키지의 Send 액션으로 메일을 발송한다. 받는 사람·제목·본문을 "
            "지정하고 파일 첨부가 가능하다. 발송 서버는 SMTP 설정 또는 Outlook 연동을 "
            "쓴다. 처리 결과 리포트를 담당자에게 자동 발송하는 마지막 단계에 흔히 쓴다."
        ),
        "keywords": ["이메일", "메일", "email", "smtp", "outlook", "발송", "첨부"],
    },
    {
        "id": "stub-pdf-extract",
        "source_type": "stub",
        "package_name": "PDF",
        "action_name": "Extract text",
        "title": "PDF — 텍스트 추출",
        "url": None,
        "content": (
            "PDF 패키지의 Extract text 액션으로 PDF 문서에서 텍스트를 추출해 변수에 "
            "담는다. 페이지 범위를 지정할 수 있고, 추출한 텍스트는 String 패키지 "
            "액션으로 파싱해 필요한 항목(금액, 날짜 등)을 뽑아낸다."
        ),
        "keywords": ["pdf", "텍스트 추출", "추출", "인보이스", "스캔"],
    },
    {
        "id": "stub-flow-control",
        "source_type": "stub",
        "package_name": "Loop / If / Variable",
        "action_name": "Loop / If / Assign",
        "title": "흐름제어 — 반복·조건 분기·변수",
        "url": None,
        "content": (
            "Loop 액션으로 목록·데이터 테이블 행·횟수 기반 반복을 구성하고, If 액션으로 "
            "조건 분기를 만든다(비교 대상 변수·연산자·값 지정). Variable 패키지의 Assign "
            "액션으로 변수에 값을 할당한다. Error handler 액션으로 단계 실패 시 처리 "
            "흐름을 지정한다."
        ),
        "keywords": ["반복", "loop", "조건", "분기", "if", "변수", "variable", "에러", "예외"],
    },
]


class FakeRetriever:
    """키워드 매칭 기반 스텁 검색기 (테스트 전용).

    source_types는 실제 검색기 계약(Retriever Protocol)에 있는 인자라 시그니처만 받는다 —
    스텁 문서엔 source_type 구분이 없어 필터링은 하지 않는다(v3 research가 넘겨도 안전).

    ⚠️ **v1~v3 전용으로 남긴다.** 필터링을 안 하므로 채널 격리(v4)를 검증할 수 없다 —
    채널을 아무리 잘게 나눠도 이 스텁 위에서는 전부 통과하는 **거짓 초록**이 된다.
    채널을 보는 테스트는 아래 `ChannelAwareFakeRetriever`를 쓴다.
    """

    def search(self, query: str, limit: int = 4, source_types: list[str] | None = None) -> list[dict]:
        q = query.lower()
        scored = []
        for doc in _FAKE_DOCS:
            matched = sum(1 for kw in doc["keywords"] if kw in q)
            if matched == 0:
                continue
            record = {k: v for k, v in doc.items() if k != "keywords"}
            record["score"] = round(matched / len(doc["keywords"]), 4)
            scored.append(record)
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# 채널 인식 검색기 스텁 (v4 — 단계별 검색 채널)
# ─────────────────────────────────────────────────────────────────────────────

# source_type이 실제로 갈리는 픽스처. 세 채널(action_schema / package_overview / doc_page)에
# **같은 업무 어휘**로 걸리는 행을 일부러 함께 둔다 — 필터가 없으면 한 질의가 세 채널 문서를
# 모두 물어오므로, 채널이 실제로 격리되는지가 여기서만 드러난다.
#
# package/action 표기는 _FAKE_ACTIONS(FakeCatalog)와 맞춘다 — research가 검색 히트를
# 카탈로그로 재조회해 메뉴를 만들기 때문에, 어긋나면 후보가 조용히 전부 탈락한다.
_FAKE_CHANNEL_DOCS: list[dict] = [
    # --- action_schema 채널 ---
    {"id": "cs-excel-open", "source_type": "action_schema",
     "package_name": "Excel_MS", "action_name": "OpenSpreadsheet",
     "title": "Excel_MS / OpenSpreadsheet — 통합 문서 열기", "url": None,
     "content": "파일 경로와 열기 모드를 받아 통합 문서를 연다.",
     "keywords": ["엑셀", "excel", "통합 문서", "연다", "열기"]},
    {"id": "cs-excel-write", "source_type": "action_schema",
     "package_name": "Excel_MS", "action_name": "writeDataTableToWorksheet",
     "title": "Excel_MS / writeDataTableToWorksheet — 데이터 테이블 쓰기", "url": None,
     "content": "데이터 테이블을 시작 셀부터 기록한다.",
     "keywords": ["엑셀", "기록", "쓴다", "데이터 테이블"]},
    {"id": "cs-email-send", "source_type": "action_schema",
     "package_name": "Email", "action_name": "sendMail",
     "title": "Email / sendMail — 메일 보내기", "url": None,
     "content": "받는 사람·제목·본문을 지정해 메일을 발송한다.",
     "keywords": ["메일", "발송", "email", "보낸다"]},
    {"id": "cs-web-open", "source_type": "action_schema",
     "package_name": "WebAutomation", "action_name": "openpage",
     "title": "WebAutomation / openpage — 페이지 열기", "url": None,
     "content": "세션에서 URL을 연다.",
     "keywords": ["브라우저", "사이트", "url", "웹"]},
    # 구제 재질의(rescue_suffix="액션") 전용 — plain 질의로는 절대 안 걸리고, 채널이
    # 0건일 때 붙이는 접미사가 있어야만 잡힌다. 구제 경로가 죽으면 이 행이 사라진다.
    {"id": "cs-rescue-only", "source_type": "action_schema",
     "package_name": "String", "action_name": "assign",
     "title": "String / assign — 값 지정", "url": None,
     "content": "변수에 값을 지정한다.",
     "keywords": ["액션"]},
    # --- package_overview 채널 (업무 분해) ---
    {"id": "cs-pkg-excel", "source_type": "package_overview",
     "package_name": "Excel_MS", "action_name": None,
     "title": "Excel_MS 패키지", "url": None,
     "content": "스프레드시트를 열고 셀을 읽고 쓰는 패키지.",
     "keywords": ["엑셀", "excel", "패키지"]},
    {"id": "cs-pkg-email", "source_type": "package_overview",
     "package_name": "Email", "action_name": None,
     "title": "Email 패키지", "url": None,
     "content": "메일 연결·발송·수신 패키지.",
     "keywords": ["메일", "email", "패키지"]},
    # 질의 정형(query_suffix) 전용 — 원문 질의로는 절대 안 걸린다. 실측에서 이 채널이
    # 26질의 중 21건 0건이었다가 접미사를 붙이자 살아난 현상의 축소판이다.
    {"id": "cs-pkg-shaped-only", "source_type": "package_overview",
     "package_name": "Sa_Ledger", "action_name": None,
     "title": "Sa_Ledger 패키지", "url": None,
     "content": "원장 조회 패키지.",
     "keywords": ["어떤 패키지 액션"]},
    # --- doc_page 채널 (파라미터) ---
    # 실제 적재와 같이 package_name·action_name이 비어 있다 — 액션을 필터로 지목할 수 없고
    # 질의어(표기+라벨)로만 집힌다는 사실을 스텁도 그대로 반영한다.
    {"id": "cs-doc-open", "source_type": "doc_page",
     "package_name": "", "action_name": "",
     "title": "Open action / 열기 작업 사용", "url": None,
     "content": "filePath에 절대 경로를, openMode에 '읽기 전용 모드'를 지정한다.",
     "keywords": ["openspreadsheet", "파라미터"]},
    {"id": "cs-doc-send", "source_type": "doc_page",
     "package_name": "", "action_name": "",
     "title": "Using Send action / 보내기 작업", "url": None,
     "content": "to·subject·message는 필수이고 sendVia로 전송 경로를 고른다.",
     "keywords": ["sendmail", "파라미터"]},
    {"id": "cs-doc-goal", "source_type": "doc_page",
     "package_name": "", "action_name": "",
     "title": "업무 자동화 개요 문서", "url": None,
     "content": "엑셀 데이터를 읽어 메일로 보내는 전형적인 흐름을 설명한다.",
     "keywords": ["엑셀", "메일"]},
]


class ChannelAwareFakeRetriever:
    """source_types를 **실제로 거르는** 스텁 검색기 (v4 채널 격리 테스트 전용).

    왜 새로 만들었나: 기존 `FakeRetriever`는 source_types를 시그니처로만 받고 무시한다.
    그 위에서는 채널을 잘못 배선해도(예: 액션 채널에 doc_page가 섞여도) 테스트가 전부
    초록이라 격리가 검증되지 않는다 — 정확히 `bot_example`이 v1~v3 내내 안 들킨 방식이다.

    실검색과 다른 점을 하나 남긴다: 여기서는 필터가 **검색 전에** 걸리지만
    (`app/services/rag.py`는 후단 필터다) 스텁은 코퍼스가 10행이라 어차피 굶지 않는다.
    후단 필터 굶주림 자체는 채널 통계(`channels.channel_stats`)로 관측한다.
    """

    def search(self, query: str, limit: int = 4, source_types: list[str] | None = None) -> list[dict]:
        q = query.lower()
        allowed = set(source_types) if source_types else None
        scored = []
        for doc in _FAKE_CHANNEL_DOCS:
            if allowed is not None and doc["source_type"] not in allowed:
                continue
            matched = sum(1 for kw in doc["keywords"] if kw in q)
            if matched == 0:
                continue
            record = {k: v for k, v in doc.items() if k != "keywords"}
            record["score"] = round(matched / len(doc["keywords"]), 4)
            scored.append(record)
        # 동점은 픽스처 순서로 — 순위 병합 테스트가 결정론이어야 한다.
        scored.sort(key=lambda d: -d["score"])
        return scored[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# 카탈로그 스텁
# ─────────────────────────────────────────────────────────────────────────────

_MISSING = object()


def _p(name, type_, required=False, options=None, label="", default=_MISSING):
    """파라미터 스펙 한 건을 만드는 헬퍼 (픽스처 가독성용)."""
    param: dict = {"name": name, "label": label or name, "type": type_, "required": required}
    if options is not None:
        param["options"] = [{"label": o, "value": o} for o in options]
    if default is not _MISSING:
        param["default"] = default
    return param


# 대표 액션 픽스처. package/action/파라미터 name은 RAG_CATALOG.md 표기를 따른다.
_FAKE_ACTIONS: list[dict] = [
    # --- WebAutomation (웹 조작) ---
    {"package": "WebAutomation", "action": "StartSessionWebAutomation", "label": "Start Session",
     "return_type": "SESSION",  # v3 세션 레지스트리 유도 테스트용 (JAR 원천과 동형)
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
    # --- Excel advanced (현행 카탈로그 — open은 세션을 리턴, 사용·닫기는 sessionName 참조) ---
    # return_type=SESSION: v3 derive_session_registry의 opener 유도 신호 (실카탈로그 동형).
    {"package": "Excel advanced", "action": "cloudExcelOpen", "label": "열기", "return_type": "SESSION",
     "parameters": [_p("fileSource", "TEXT", False, label="파일"),
                    _p("sessionName", "TEXT", False, label="세션 이름")]},
    {"package": "Excel advanced", "action": "excelAdvancedPackageSaveWorkbookAction", "label": "저장",
     "parameters": [_p("sessionName", "TEXT", True, label="세션 이름")]},
    {"package": "Excel advanced", "action": "excelAdvancedPackageCloseAction", "label": "닫기",
     "parameters": [_p("sessionName", "TEXT", True, label="세션 이름")]},
    # --- 흐름 제어 (컨테이너) — 현행 카탈로그(llm_agent 소싱) 표기 (RPA-141) ---
    {"package": "Loop", "action": "cloudUsingLoopAction", "label": "루프",
     "parameters": [_p("iteratorType", "SELECT", True,
                       ["For each row in a data table", "N times", "For each item in list"], "반복 유형")]},
    {"package": "Loop", "action": "loopPackageBreakAction", "label": "Break", "parameters": []},
    {"package": "If", "action": "ifPackageElseAction", "label": "Else", "parameters": []},
    {"package": "If", "action": "ifPackageElseIfOptionalAction", "label": "Else If",
     "parameters": [_p("condition", "CONDITION", True, label="조건")]},
    {"package": "Step", "action": "stepAction", "label": "단계",
     "parameters": [_p("title", "TEXT", False, label="제목")]},
    {"package": "Error handler", "action": "errorHandlerTry", "label": "Try", "parameters": []},
    {"package": "Error handler", "action": "errorHandlerCatch", "label": "Catch",
     "parameters": [_p("exception", "EXCEPTION", True, label="예외"),
                    _p("continueOnError", "CHECKBOX", False, label="오류 발생 시 계속")]},
    {"package": "Error handler", "action": "errorHandlerFinally", "label": "Finally", "parameters": []},
    {"package": "Error handler", "action": "errorHandlerThrow", "label": "Throw", "parameters": []},
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
    """대표 액션 픽스처 기반 스텁 조회기 (테스트 전용)."""

    def get_action_schema(self, package: str, action: str) -> dict | None:
        return _FAKE_INDEX.get((package, action))

    def iter_action_schemas(self):
        """전체 스펙 순회 — v3 세션 레지스트리 유도·라벨 인덱스가 쓴다 (BackendCatalog 동형)."""
        yield from _FAKE_INDEX.values()
