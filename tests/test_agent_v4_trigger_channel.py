"""트리거 채널 편입 (RPA-298 Phase 3) — 검색은 메뉴를 **좁히지 않고 순서만** 준다.

검증 대상은 넷이다.

1. **채널 정의** — `TRIGGER`가 다른 채널과 소스 타입을 나눠 갖고, 굶주림 감지기가 이 채널의
   턴당 질의 수(1건) 안에서 실제로 도달 가능한 임계를 쓴다.
2. **순서만 준다** — 검색 상위가 메뉴 위로 올라오되 메뉴는 **전량 그대로**다. 9문서짜리
   폐쇄 어휘에서 검색이 거르기 시작하면 정답이 상위 k 밖으로 밀리는 실패가 생긴다.
3. **없어도 안 죽는다** — 검색기 부재(사용자 제공 카탈로그)·검색 실패·구 카탈로그(트리거 0행)
   전부 예전 동작(카탈로그 순서 / 조용한 휴면)으로 물러선다.
4. **계약 유지** — `recommend_trigger(spec, document)` 시그니처와 폐쇄 어휘 검사는 그대로다.
   generate.py가 위치 인자 2개로 부른다.

⚠️ `tests/agent_stubs.py`의 픽스처에는 `trigger_schema` 행이 없어(액션·문서 채널용) 여기서
로컬 스텁을 만든다. LLM(`chat_json`)은 전량 몽키패치한다 — 검증하는 건 결정론 배선이다.
"""

import inspect

import pytest

import app.agent.v4.orchestrator.triggers as trig_mod
from app.agent.knowledge.channels import (
    CHANNELS,
    LOADED_SOURCE_TYPES,
    TRIGGER,
    channel_stats,
    reset_channel_stats,
)

# 한 턴에 트리거 채널이 던지는 질의 수. 의도 게이트를 통과한 턴에만, 의도 조각을 합쳐
# **한 번** 던진다 — warn_after가 이 값을 넘으면 감지기가 한 턴 안에서 못 울린다.
_QUERIES_PER_TURN = 1


@pytest.fixture(autouse=True)
def _clean_channel_state():
    """채널 집계는 프로세스 전역이다 — 테스트 간 누수를 막는다."""
    reset_channel_stats()
    yield
    reset_channel_stats()


# 카탈로그 순서(package_name, title, chunk_index)를 흉내 낸 픽스처. 검색이 뒤집을 수 있게
# 알파벳 앞선 패키지를 일부러 맨 위에 둔다.
_ROWS = [
    {"package": "AISense trigger", "title": "Creating an AISense trigger",
     "url": "/ai", "content": "화면에 특정 이미지가 나타나면 봇을 실행한다."},
    {"package": "Email trigger", "title": "Creating an email trigger",
     "url": "/mail", "content": "메일이 수신되면 봇을 실행한다."},
    {"package": "File Folder trigger", "title": "Creating a file and folder trigger",
     "url": "/file", "content": "폴더에 파일이 생기거나 변경되면 봇을 실행한다."},
    {"package": "Hot key trigger", "title": "Creating a hot key trigger",
     "url": "/hotkey", "content": "지정한 단축키를 누르면 봇을 실행한다."},
]


class _Catalog:
    """`list_trigger_schemas`만 있는 최소 카탈로그 (BackendCatalog와 같은 반환 계약)."""

    def __init__(self, rows):
        self._rows = rows

    def list_trigger_schemas(self):
        return [dict(r) for r in self._rows]


class _TriggerRetriever:
    """`trigger_schema`만 돌려주는 로컬 스텁 검색기.

    실적재를 흉내 내 **한 문서를 청크 2행으로** 낸다(31행 / 9문서 = 평균 3.4청크). 문서
    단위로 접지 않으면 한 패키지가 순위를 통째로 먹어 재정렬이 무의미해진다.
    """

    def __init__(self, order=("Email trigger", "File Folder trigger"), boom=False):
        self.calls: list[dict] = []
        self._order = list(order)
        self._boom = boom

    def search(self, query, limit=4, source_types=None):
        self.calls.append({"query": query, "limit": limit, "source_types": source_types})
        if self._boom:
            raise RuntimeError("opensearch down")
        if source_types is not None and "trigger_schema" not in source_types:
            return []
        hits = []
        for pkg in self._order:
            row = next(r for r in _ROWS if r["package"] == pkg)
            for chunk in (0, 1):
                hits.append({
                    "id": f"{pkg}#{chunk}", "source_type": "trigger_schema",
                    "package_name": pkg, "action_name": None,
                    "title": row["title"], "url": row["url"], "content": row["content"],
                    "score": round(0.9 - 0.05 * len(hits), 3),
                })
        return hits[:limit]


def _pick(**kw):
    d = {"none": False, "kind": "trigger", "package": "Email trigger",
         "title": "Creating an email trigger", "reason": "'메일이 오면' 요구",
         "setup_hint": "Control Room에서 이메일 트리거 연결"}
    d.update(kw)
    return trig_mod._TriggerPick(**d)


def _wire(monkeypatch, rows=_ROWS, retriever=None, pick=None):
    """카탈로그·검색기·LLM을 결정론으로 고정하고, 프롬프트를 담을 리스트를 돌려준다."""
    prompts: list[str] = []
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Catalog(rows))
    monkeypatch.setattr(trig_mod, "get_retriever", lambda: retriever)

    def fake_chat(messages, **kw):
        prompts.append(messages[1]["content"])
        return pick if pick is not None else _pick()

    monkeypatch.setattr(trig_mod, "chat_json", fake_chat)
    return prompts


def _menu_packages(prompt: str) -> list[str]:
    """프롬프트 메뉴의 **트리거 패키지** 순서 (`- [패키지] 제목: 본문` 형식).

    Control Room 스케줄 줄은 카탈로그 행이 아니라 상시 추가되는 고정 항목이라 뺀다.
    """
    names = [line.split("]", 1)[0][3:] for line in prompt.splitlines() if line.startswith("- [")]
    return [n for n in names if n != "스케줄"]


# ─────────────────────────────────────────────────────────────────────────────
# (A) 채널 정의
# ─────────────────────────────────────────────────────────────────────────────

def test_trigger_channel_is_registered_and_isolated():
    """`trigger_schema`는 어느 채널에도 안 들어가 있었다 — 편입되면서 격리도 지켜져야 한다."""
    assert TRIGGER in CHANNELS
    assert TRIGGER.source_types == ("trigger_schema",)
    assert set(TRIGGER.source_types) <= LOADED_SOURCE_TYPES

    others = [c for c in CHANNELS if c is not TRIGGER]
    for channel in others:
        assert not (set(channel.source_types) & set(TRIGGER.source_types)), \
            f"{channel.name}과 소스 타입이 겹친다"
    assert len({c.name for c in CHANNELS}) == len(CHANNELS)


def test_trigger_warn_threshold_is_reachable_within_one_turn():
    """임계가 한 턴 질의 수보다 크면 이 채널의 굶주림 감지기는 사실상 꺼져 있다.

    트리거는 턴당 1질의뿐이라 공통값(6)이나 다른 채널 값(3·5·8)을 쓰면 **영원히 못 울린다**.
    """
    assert TRIGGER.warn_after <= _QUERIES_PER_TURN


def test_trigger_channel_has_no_rescue_requery():
    """0건이 곧 고장 신호인 채널이다 — 구제 재질의가 어쩌다 살려내면 고장을 가린다.

    메뉴는 검색과 무관하게 전량이라 굶어도 기능이 줄지 않는다: 뚫을 이유가 없고 드러낼
    이유만 있다.
    """
    assert TRIGGER.rescue("메일이 오면") is None


# ─────────────────────────────────────────────────────────────────────────────
# (B) 검색 편입 — 순서만 준다
# ─────────────────────────────────────────────────────────────────────────────

def test_intent_turn_searches_the_trigger_channel(monkeypatch):
    """편입 전에는 어느 경로에서도 `trigger_schema`를 검색하지 않았다 — 실제로 나가는지 본다."""
    retriever = _TriggerRetriever()
    _wire(monkeypatch, retriever=retriever)

    trig_mod.recommend_trigger({"goal": "메일이 오면 첨부를 저장한다", "requirements": []}, None)

    assert len(retriever.calls) == _QUERIES_PER_TURN
    assert retriever.calls[0]["source_types"] == ["trigger_schema"]
    assert retriever.calls[0]["limit"] == TRIGGER.limit
    # 채널 집계에 잡혀야 굶주림 감지기가 이 경로를 본다(통계만 비면 실전에서 안 울린다).
    assert channel_stats()[TRIGGER.name]["searches"] == 1


def test_search_rank_reorders_the_menu_but_keeps_it_whole(monkeypatch):
    """🔴 핵심 계약 — 검색은 **올리는 기준**이지 거르는 기준이 아니다.

    9문서짜리 폐쇄 어휘에서 검색으로 좁히면 정답이 상위 k 밖으로 밀리는 실패가 새로 생긴다.
    (재정렬만 하면 검색이 틀려도 손해가 위치 편향뿐이다.)
    """
    prompts = _wire(monkeypatch, retriever=_TriggerRetriever(
        order=("File Folder trigger", "Email trigger")))

    trig_mod.recommend_trigger({"goal": "폴더에 파일이 생기면 처리한다", "requirements": []}, None)

    menu = _menu_packages(prompts[0])
    assert menu[:2] == ["File Folder trigger", "Email trigger"], "검색 순위가 메뉴 위로 안 왔다"
    # 검색에 안 걸린 행도 남는다 — 카탈로그 순서 그대로 뒤에 붙는다.
    assert menu == ["File Folder trigger", "Email trigger", "AISense trigger", "Hot key trigger"]
    assert "[스케줄]" in prompts[0]  # Control Room 스케줄 줄은 상시 포함


def test_chunks_of_one_document_do_not_eat_the_ranking(monkeypatch):
    """같은 문서의 2·3번째 청크가 따로 순위를 먹으면 한 패키지가 상위를 통째로 차지한다.

    카탈로그는 (package, title)로 접힌 9행이고 검색은 31청크를 그대로 준다 — 접는 키가
    없으면 히트 4건이 문서 2개가 아니라 '자리 4개'가 된다.
    """
    prompts = _wire(monkeypatch, retriever=_TriggerRetriever())

    # 전제: 검색은 문서 2개를 청크 4행으로 준다.
    probe = _TriggerRetriever().search("q", limit=TRIGGER.limit, source_types=["trigger_schema"])
    assert len(probe) == 4 and len({h["title"] for h in probe}) == 2

    trig_mod.recommend_trigger({"goal": "메일이 오면 처리한다", "requirements": []}, None)

    assert _menu_packages(prompts[0]) == [
        "Email trigger", "File Folder trigger", "AISense trigger", "Hot key trigger",
    ]


def test_rank_query_carries_intent_fragments_not_the_whole_document(monkeypatch):
    """순위 질의는 **시점 문장**이어야 한다 — 원문 1500자를 던지면 신호가 업무 서술에 묻힌다."""
    retriever = _TriggerRetriever()
    _wire(monkeypatch, retriever=retriever)

    document = (
        "이 업무는 회계팀이 월말에 수행한다.\n"
        "담당자는 ERP에서 전표를 내려받아 엑셀로 정리한다.\n"
        "메일이 오면 첨부파일을 지정 폴더에 저장한다.\n"
    )
    trig_mod.recommend_trigger(
        {"goal": "첨부 저장 자동화", "requirements": [{"text": "결과를 팀장에게 보고한다"}]},
        document,
    )

    query = retriever.calls[0]["query"]
    assert "메일이 오면" in query
    assert "ERP에서 전표를" not in query, "의도 없는 절차 문장이 질의를 희석한다"
    assert "결과를 팀장에게 보고한다" not in query
    assert len(query) <= trig_mod._MAX_QUERY_CHARS


def test_evidence_follows_the_top_ranked_document_of_the_picked_package(monkeypatch):
    """LLM은 package만 고른다 — 같은 패키지에 문서가 여럿이면 순위가 근거 문서를 정한다.

    재정렬 전에는 카탈로그 첫 행(제목 알파벳순)이 무조건 근거였다.
    """
    rows = [
        {"package": "Email trigger", "title": "Creating an email trigger",
         "url": "/mail/create", "content": "이메일 트리거 만들기"},
        {"package": "Email trigger", "title": "Email trigger with Gmail",
         "url": "/mail/gmail", "content": "Gmail 계정으로 메일 수신을 감지한다"},
    ]

    class _GmailFirst(_TriggerRetriever):
        def search(self, query, limit=4, source_types=None):
            self.calls.append({"query": query, "limit": limit, "source_types": source_types})
            return [{"id": "g#0", "source_type": "trigger_schema", "package_name": "Email trigger",
                     "action_name": None, "title": "Email trigger with Gmail",
                     "url": "/mail/gmail", "content": "Gmail", "score": 0.9}]

    _wire(monkeypatch, rows=rows, retriever=_GmailFirst())
    out = trig_mod.recommend_trigger(
        {"goal": "Gmail에 메일이 오면 저장한다", "requirements": []}, None)

    assert out["sources"][0]["title"] == "Email trigger with Gmail"
    assert out["sources"][0]["url"] == "/mail/gmail"
    assert out["title"] == "Email trigger with Gmail"  # 표기는 카탈로그 canonical 값 그대로


# ─────────────────────────────────────────────────────────────────────────────
# (C) 없어도 안 죽는다 — 하위호환
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_retriever_keeps_catalog_order_and_still_recommends(monkeypatch):
    """사용자 제공 카탈로그 경로는 검색기가 없다 — 터지지 않고 예전 동작(카탈로그 순서)이다."""
    prompts = _wire(monkeypatch, retriever=None)

    out = trig_mod.recommend_trigger({"goal": "메일이 오면 처리한다", "requirements": []}, None)

    assert out["package"] == "Email trigger"
    assert _menu_packages(prompts[0]) == [r["package"] for r in _ROWS]
    assert TRIGGER.name not in channel_stats(), "검색을 안 던졌으면 집계도 없어야 한다"


def test_retriever_construction_failure_degrades_to_catalog_order(monkeypatch):
    """검색기 확보 자체가 터져도(인프라 부재) 트리거 제안은 살아 있어야 한다."""
    def _boom():
        raise RuntimeError("no retriever")

    prompts = _wire(monkeypatch)
    monkeypatch.setattr(trig_mod, "get_retriever", _boom)

    out = trig_mod.recommend_trigger({"goal": "메일이 오면 처리한다", "requirements": []}, None)

    assert out["package"] == "Email trigger"
    assert _menu_packages(prompts[0]) == [r["package"] for r in _ROWS]


def test_search_failure_degrades_to_catalog_order(monkeypatch):
    """검색 호출이 터져도 마찬가지 — 순위는 부가 신호지 전제 조건이 아니다."""
    prompts = _wire(monkeypatch, retriever=_TriggerRetriever(boom=True))

    out = trig_mod.recommend_trigger({"goal": "메일이 오면 처리한다", "requirements": []}, None)

    assert out["package"] == "Email trigger"
    assert _menu_packages(prompts[0]) == [r["package"] for r in _ROWS]
    assert channel_stats()[TRIGGER.name]["hits"] == 0  # 실패도 굶주림으로 집계된다


def test_old_catalog_without_trigger_rows_never_searches(monkeypatch):
    """트리거 0행(구 카탈로그)이면 조용히 쉰다 — 순위를 매길 대상이 없으니 검색도 안 던진다."""
    retriever = _TriggerRetriever()
    _wire(monkeypatch, rows=[], retriever=retriever,
          pick=_pick(kind="schedule", package=None, title="Control Room 예약 실행"))

    out = trig_mod.recommend_trigger(
        {"goal": "매일 아침 9시에 보고서를 만든다", "requirements": []}, None)

    assert out["kind"] == "schedule" and out["sources"] == []
    assert retriever.calls == [], "메뉴가 비었는데 검색을 던지면 왕복만 늘어난다"
    assert TRIGGER.name not in channel_stats()


def test_no_intent_skips_both_search_and_llm(monkeypatch):
    """결정론 의도 게이트는 그대로다 — 시점 표현이 없으면 검색도 LLM도 비용 0."""
    retriever = _TriggerRetriever()
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Catalog(_ROWS))
    monkeypatch.setattr(trig_mod, "get_retriever", lambda: retriever)

    def _boom(*a, **k):
        raise AssertionError("의도 없음이면 LLM을 부르면 안 된다")

    monkeypatch.setattr(trig_mod, "chat_json", _boom)

    assert trig_mod.recommend_trigger({"goal": "엑셀 자료 정리", "requirements": []}, None) is None
    assert retriever.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# (D) 계약 유지
# ─────────────────────────────────────────────────────────────────────────────

def test_pick_outside_menu_is_still_rejected_after_reordering(monkeypatch):
    """재정렬은 순서만 바꾼다 — 폐쇄 어휘 검사가 볼 **집합**은 그대로여야 한다."""
    _wire(monkeypatch, retriever=_TriggerRetriever(), pick=_pick(package="Invented trigger"))
    assert trig_mod.recommend_trigger(
        {"goal": "메일이 오면 처리한다", "requirements": []}, None) is None


def test_ranked_menu_is_a_permutation_not_a_filter(monkeypatch):
    """검색이 한 건만 물어와도 메뉴에서 나머지가 사라지면 안 된다 (거르기 금지의 최소 단위)."""
    class _OneHit(_TriggerRetriever):
        def search(self, query, limit=4, source_types=None):
            self.calls.append({"query": query, "limit": limit, "source_types": source_types})
            return [{"id": "h#0", "source_type": "trigger_schema",
                     "package_name": "Hot key trigger", "action_name": None,
                     "title": "Creating a hot key trigger", "url": "/hotkey",
                     "content": "단축키", "score": 0.9}]

    monkeypatch.setattr(trig_mod, "get_retriever", lambda: _OneHit())
    ranked = trig_mod._ranked_menu([dict(r) for r in _ROWS], "단축키를 누르면 실행한다")

    assert ranked[0]["package"] == "Hot key trigger"
    assert sorted(r["package"] for r in ranked) == sorted(r["package"] for r in _ROWS)


def test_signature_stays_positional_two_args():
    """generate.py가 `to_thread(recommend_trigger, spec, document)`로 부른다 — 인자를 늘리면 깨진다."""
    params = list(inspect.signature(trig_mod.recommend_trigger).parameters)
    assert params == ["spec", "document"]
