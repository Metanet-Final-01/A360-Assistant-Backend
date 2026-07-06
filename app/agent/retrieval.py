"""RAG 검색 인터페이스와 스텁 구현.

실제 검색(pgvector·하이브리드)은 RAG 담당 영역이다(RPA-9 진행 중). agent 코드는
`Retriever` 인터페이스에만 의존하므로, RAG 담당 모듈이 완성되면 `get_retriever()`가
그 구현을 반환하도록 이 파일만 교체한다 — graph.py 등 나머지는 수정하지 않는다.

검색 결과 dict는 백엔드 `/api/rag/search`(`app.rag.store.db.search`)의 행 스키마를
따른다: id, source_type, package_name, action_name, title, url, content, score.
실제 구현도 같은 스키마로 반환하면 그대로 호환된다.
"""

import os
from typing import Protocol


class Retriever(Protocol):
    """검색 구현이 따라야 하는 계약. score 내림차순으로 최대 limit개를 반환한다."""

    def search(self, query: str, limit: int = 4) -> list[dict]: ...


# A360 코어 패키지 축약 카탈로그. 실제 KB가 붙기 전 개발·데모용 최소 데이터로,
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
    """키워드 매칭 기반 가짜 검색기. RAG 담당 모듈이 나오기 전까지의 임시 구현."""

    def search(self, query: str, limit: int = 4) -> list[dict]:
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


_fake_retriever: Retriever = FakeRetriever()


def get_retriever() -> Retriever:
    """graph가 쓰는 검색기.

    AGENT_RETRIEVER=hybrid이면 실제 RAG 하이브리드 검색기(app.services.agent_retriever)를,
    아니면 FakeRetriever(스텁)를 반환한다. 기본은 fake라 인프라(pgvector·OpenSearch)가
    없는 CI·로컬에서도 그래프가 동작한다 — 실제 KB를 쓰려면 RAG 인프라를 띄우고
    AGENT_RETRIEVER=hybrid로 설정한다.
    """
    if os.getenv("AGENT_RETRIEVER", "fake").lower() == "hybrid":
        from app.services.agent_retriever import get_hybrid_retriever

        return get_hybrid_retriever()
    return _fake_retriever
