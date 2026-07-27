# -*- coding: utf-8 -*-
"""요구별 질의 커버리지 감사 (RPA-298, 2026-07-27 실사용 결함).

## 무엇을 막는가

업무정의서 Task 1·2는 요구 두 개다 — "증권 버튼을 클릭한다", "'국내 금' 항목을 클릭한다".
그런데 질의 확장(LLM)이 이 둘을 **"브라우저에서 네이버 금융 국내 금 시세 페이지 열기"**
한 질의로 뭉쳤다. 액션 채널이 던진 질의 8건 어디에도 '클릭'이 없었고, 메뉴 12,973자에
클릭 액션이 **0건**이었다.

카탈로그에는 클릭 액션이 19개 있고 검색도 정상적으로 올린다 — **찾아달라고 물어보질
않은 것**이다. 폐쇄 어휘라 메뉴에 없으면 쓸 수 없어, LLM은 그 자리를 실행되지 않는
`Step` 구획으로 비웠다(17개 중 9개). 뒤 단계가 복구할 수 없는 손실이다.

## 이 판정이 지켜야 하는 두 방향

**놓치면 안 된다** — 실측 사례(클릭 요구 2건)가 잡혀야 한다.
**남발하면 안 된다** — must 전부를 구제하면 질의가 배로 늘고 비용이 그만큼 든다.
   실측에서 처음 구현은 must 7건을 **전부** 구제 대상으로 잡았다(토큰화 결함).
"""

import pytest

from app.agent.v4.recommend import research
from app.agent.v4.recommend.research import (
    _covers,
    _sem_tokens,
    _stem,
    rescue_en_queries,
    uncovered_requirements,
)


def _spec(*reqs):
    return {"goal": "g", "requirements": [
        {"req_id": f"req-{i}", "text": t, "priority": p}
        for i, (t, p) in enumerate(reqs, 1)
    ]}


# 실측 그대로 — 파이프라인이 실제로 만든 질의 8건
_REAL_QUERIES = [
    "브라우저에서 네이버 금융 국내 금 시세 페이지 열기",
    "browser open Naver finance domestic gold page",
    "Excel table border formatting apply",
    "엑셀 표 영역에 테두리 서식 적용",
    "daily price extract last 3 days excel table input",
    "일별 시세 최근 3일 추출해 엑셀 표에 입력",
    "엑셀 결과물 저장하고 메일에 첨부해 발송",
    "Excel save attach send email",
]

_REAL_SPEC = _spec(
    ("브라우저에서 네이버 웹페이지에 접속한다.", "must"),
    ("네이버 메인 화면에서 증권 버튼을 클릭한다.", "must"),
    ("증권 페이지에서 국내 금 항목을 클릭한다.", "must"),
    ("국내 금의 일별 시세 표를 엑셀에 입력한다.", "must"),
    ("최근 3일치 일별 시세를 가져온다.", "must"),
    ("엑셀에서 시세 표의 테두리를 설정한다.", "must"),
    ("엑셀 표를 메일로 발송한다.", "must"),
)


# ── 실측 재현 ────────────────────────────────────────────────────────────────

def test_실측_결함이_잡힌다():
    """🔴 이 파일의 존재 이유 — 클릭 요구 2건이 질의를 못 받았다."""
    missing = uncovered_requirements(_REAL_SPEC, _REAL_QUERIES)
    assert [r["req_id"] for r in missing] == ["req-2", "req-3"]
    assert all("클릭" in r["text"] for r in missing)


def test_다뤄진_요구는_구제하지_않는다():
    """🔴 남발 방지. 처음 구현은 must 7건을 **전부** 잡았다(토큰화가 분모를 부풀림).

    질의가 실제로 다룬 5건이 구제 대상에 들어오면 비용이 배가 되고, 같은 검색을
    두 번 하게 된다.
    """
    missing_ids = {r["req_id"] for r in uncovered_requirements(_REAL_SPEC, _REAL_QUERIES)}
    for rid in ("req-1", "req-4", "req-5", "req-6", "req-7"):
        assert rid not in missing_ids, f"{rid}은 질의가 다뤘는데 구제 대상이 됐다"


# ── 어간 절단 — 조사만 다른 어절 ────────────────────────────────────────────

@pytest.mark.parametrize("word,expected", [
    ("표를", "표"), ("표에", "표"),          # 🔴 접두 비교만으로는 서로 안 맞는다
    ("메일로", "메일"), ("메일에", "메일"),
    ("엑셀에서", "엑셀"),
    ("클릭한다", "클릭"), ("발송한다", "발송"),
    ("버튼을", "버튼"),
    ("증권", "증권"),                        # 뗄 게 없으면 그대로
    ("excel", "excel"),                      # 영문은 안 건드린다
])
def test_어간_절단(word, expected):
    assert _stem(word) == expected


def test_조사만_다른_어절이_같은_것으로_읽힌다():
    """'표를'과 '표에'는 어느 쪽도 다른 쪽의 접두가 아니다 — 어간 절단이 없으면 못 맞춘다."""
    assert _covers(_stem("표를"), _sem_tokens("엑셀 표에 입력"))
    assert _covers(_stem("메일로"), _sem_tokens("메일에 첨부해 발송"))


def test_어절마다_토큰_하나만_담는다():
    """🔴 어간 근사형을 원형과 **함께** 담으면 분모만 불어 비율이 거짓으로 낮아진다.

    실측에서 이 때문에 "엑셀 표 테두리"가 겹치는 요구조차 18%로 계산돼 미커버가 됐다.
    """
    assert _sem_tokens("클릭한다") == {"클릭"}
    assert len(_sem_tokens("엑셀에서 시세 표의 테두리를 설정한다")) == 5


# ── 판정 경계 ────────────────────────────────────────────────────────────────

def test_should_요구는_구제하지_않는다():
    """빠져도 흐름이 성립하고, 전부 구제하면 질의가 배로 는다."""
    spec = _spec(("로그를 파일에 남긴다.", "should"))
    assert uncovered_requirements(spec, ["엑셀을 연다"]) == []


def test_의미_토큰이_없는_요구는_건너뛴다():
    """기호·숫자만 있는 요구는 판정 근거가 없다 — 조용히 넘긴다."""
    spec = _spec(("...", "must"), ("3", "must"))
    assert uncovered_requirements(spec, ["엑셀을 연다"]) == []


def test_질의가_하나도_없으면_must_전부가_구제_대상이다():
    """질의 확장이 통째로 실패한 경우 — 요구 원문으로라도 검색해야 한다."""
    missing = uncovered_requirements(_REAL_SPEC, [])
    assert len(missing) == 7


def test_요구_원문이_그대로_질의면_커버로_읽는다():
    """강등 경로(_expand_queries 실패 시 요구 텍스트를 질의로 씀)가 다시 구제되면 중복이다."""
    spec = _spec(("네이버 메인 화면에서 증권 버튼을 클릭한다.", "must"))
    assert uncovered_requirements(spec, ["네이버 메인 화면에서 증권 버튼을 클릭한다."]) == []


def test_영문_질의도_커버로_읽는다():
    """한/영 이중 질의라 영어 쪽만 요구를 다루는 경우가 있다."""
    spec = _spec(("excel workbook open", "must"))
    assert uncovered_requirements(spec, ["excel workbook open"]) == []


# ── 구제 질의의 영어 짝 ──────────────────────────────────────────────────────
#
# 🔴 한국어 구제 질의만으로는 **부족하다**(실측 순위, 2026-07-27):
#   "네이버 메인 화면에서 증권 버튼을 클릭한다."  → Recorder/Click 순위 없음
#   "click the stock button on the Naver main page" → Recorder/Click 3위
# 한국어를 일반화해도("웹 페이지에서 버튼을 클릭한다") 안 올라온다 — 액션 식별자가
# 영어라 어휘 검색이 영어 쪽에서만 걸린다. 그래서 구제도 한/영 쌍으로 던진다.

_CLICKS = [_REAL_SPEC["requirements"][1], _REAL_SPEC["requirements"][2]]


def _plan(*pairs):
    return research._RescuePlan(
        queries=[research._RescueQuery(req_id=r, en_query=q) for r, q in pairs]
    )


def _stub(monkeypatch, plan, seen=None):
    def fake(messages, *, purpose, model_cls):
        if seen is not None:
            seen.update(prompt=messages[0]["content"], user=messages[1]["content"],
                        purpose=purpose, model_cls=model_cls)
        return plan
    monkeypatch.setattr(research, "chat_json", fake)


def test_영어_구제_질의를_받아온다(monkeypatch):
    seen = {}
    _stub(monkeypatch, _plan(("req-2", "click a button on a web page"),
                             ("req-3", "click a link item on a web page")), seen)

    assert rescue_en_queries(_REAL_SPEC, _CLICKS) == [
        "click a button on a web page", "click a link item on a web page",
    ]
    # 🔴 구제 대상 요구**만** 넘겨야 한다 — 전량을 넘기면 처음 뭉갠 상황이 그대로 재현된다.
    assert "req-2" in seen["user"] and "req-3" in seen["user"]
    assert "req-1" not in seen["user"]
    # 사용량이 purpose로 귀속돼야 한다(링 게이지 계약)
    assert seen["purpose"] == "recommend"


def test_전용_프롬프트를_쓴다(monkeypatch):
    """🔴 `_expand_queries` 재사용은 실측에서 기각됐다 — 조작 동사 없이 도메인만 번역했다.

    실제 출력: "Naver finance stock domestic gold daily prices" (클릭 요구 2건 → 질의 1건,
    'click' 없음). 그 프롬프트의 규칙 "같은 기능이면 한 단위로 묶으세요"가 이 결함을 처음
    만든 규칙이라, 같은 프롬프트로 구제하면 같은 방식으로 실패한다.
    """
    seen = {}
    _stub(monkeypatch, _plan(("req-2", "click a button on a web page")), seen)
    rescue_en_queries(_REAL_SPEC, _CLICKS)

    assert seen["prompt"] is research._RESCUE_PROMPT
    assert seen["prompt"] is not research._PROMPT


def test_요구_수보다_많은_질의는_받지_않는다(monkeypatch):
    """한 요구를 여러 갈래로 풀면 검색 팬아웃이 구제 대상 수와 무관하게 분다."""
    _stub(monkeypatch, _plan(("req-2", "click a button"), ("req-2", "press a button"),
                             ("req-3", "click a link"), ("req-3", "tap a link")))
    assert len(rescue_en_queries(_REAL_SPEC, _CLICKS)) == 2


def test_중복_질의는_접는다(monkeypatch):
    """같은 조작이 두 요구에서 나올 수 있다 — 같은 검색을 두 번 할 이유가 없다."""
    _stub(monkeypatch, _plan(("req-2", "click a button on a web page"),
                             ("req-3", "Click a Button on a Web Page")))
    assert rescue_en_queries(_REAL_SPEC, _CLICKS) == ["click a button on a web page"]


def test_번역이_실패해도_추천을_잃지_않는다(monkeypatch):
    """🔴 최선 노력 경로다. 여기서 예외가 새면 감사가 **없던 때보다 나빠진다**."""
    def boom(messages, *, purpose, model_cls):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(research, "chat_json", boom)
    assert rescue_en_queries(_REAL_SPEC, _CLICKS) == []


def test_구제_대상이_없으면_호출하지_않는다(monkeypatch):
    """감사가 깨끗하면 LLM 비용이 0이어야 한다 — 이 경로는 결함이 증명된 뒤에만 돈다."""
    called = []
    monkeypatch.setattr(research, "chat_json",
                        lambda *a, **k: called.append(1) or _plan())
    assert rescue_en_queries(_REAL_SPEC, []) == []
    assert rescue_en_queries(_REAL_SPEC, [{"req_id": "x", "text": "  "}]) == []
    assert called == []


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_dossier가_구제_질의를_실제로_던진다():
    """감사만 하고 질의를 안 늘리면 아무것도 달라지지 않는다."""
    import inspect

    src = inspect.getsource(research.build_dossier)
    assert "uncovered_requirements(spec, queries)" in src
    assert "queries.extend(rescue_queries)" in src
    # 액션 채널 검색보다 **앞에** 있어야 구제 질의가 실제로 검색된다
    assert src.index("queries.extend") < src.index("gather_channel(retriever, channels.ACTION")


def test_dossier가_영어_짝도_함께_던진다():
    """한국어 원문만 늘리면 실측 사례에서 Recorder/Click이 여전히 후보에 못 든다."""
    import inspect

    src = inspect.getsource(research.build_dossier)
    assert "rescue_en_queries" in src
    # 동기 LLM이라 이벤트 루프를 잡으면 같은 워커의 다른 턴까지 멈춘다
    assert "to_thread(rescue_en_queries" in src
