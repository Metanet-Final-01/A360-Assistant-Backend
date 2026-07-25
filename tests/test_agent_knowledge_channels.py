"""단계별 검색 채널 격리 (RPA-298, 설계 §4 Phase 1 · 9~10단계).

검증 대상은 셋이다.

1. **격리** — 단계마다 자기 소스 타입만 본다. doc_page가 코퍼스의 91%(실측 16,164/17,838)라
   섞이면 액션 랭킹을 통째로 덮는다.
2. **침묵 금지** — 연속 0건이 이어지는 채널은 굶주림 구간당 1회 WARN한다.
   `bot_example`이 v1~v3 내내 0건인 채 아무 신호도 없었던 게 이 장치를 만든 이유다.
   누적이 아니라 **연속**을 세야 채널이 나중에 죽는 경우(재적재·개명)도 잡힌다.
3. **점수 비교 금지** — 채널·질의를 가로질러 점수를 비교하지 않는다. 리랭커가 한 질의만
   폴백해도(rrf_score ≈0.03 vs rerank_score ≈0.8) 그 질의 후보가 통째로 밀려나기 때문.

⚠️ 이 파일은 `ChannelAwareFakeRetriever`를 쓴다. conftest의 기본 `FakeRetriever`는
source_types를 **거르지 않아** 채널을 잘못 배선해도 초록이 나온다(거짓 초록).
LLM은 전부 몽키패치한다 — 검증하는 것은 결정론이지 모델 출력이 아니다.
"""

import asyncio

import pytest

from app.agent.knowledge import channels
from app.agent.knowledge.channels import (
    ACTION,
    CHANNELS,
    DECOMPOSE,
    LOADED_SOURCE_TYPES,
    PARAM_DOC,
    SearchChannel,
    channel_for_source_types,
    channel_stats,
    gather_channel,
    merge_by_rank,
    record_hits,
    reset_channel_stats,
    reset_search_gates,
    search_channel,
)
from app.agent.v4.catalog_context import CatalogContext, OverlayRetriever
from app.agent.v4.recommend import research as research_mod
from app.agent.v4.recommend.research import _OperationPlan, _ResearchPlan, build_dossier
from tests.agent_stubs import ChannelAwareFakeRetriever, FakeCatalog


@pytest.fixture(autouse=True)
def _clean_channel_state():
    """채널 집계·세마포어는 프로세스 전역이다 — 테스트 간 누수를 막는다."""
    reset_channel_stats()
    reset_search_gates()
    yield
    reset_channel_stats()
    reset_search_gates()


@pytest.fixture
def retriever():
    return ChannelAwareFakeRetriever()


def _spec(goal="엑셀 데이터를 읽어 메일로 보낸다", *texts):
    return {
        "goal": goal,
        "requirements": [
            {"req_id": f"req-{i}", "text": t, "priority": "must", "source": "doc"}
            for i, t in enumerate(texts or ("엑셀 통합 문서를 연다", "메일을 발송한다"), 1)
        ],
    }


def _stub_research_llm(monkeypatch, queries=("엑셀", "메일")):
    """조작 단위·질의 확장 LLM을 결정론 스텁으로 — API 한도와 무관하게 돌아야 한다."""
    from app.agent.v4.recommend.research import _ResearchUnit

    def fake(messages, *, purpose, model_cls):
        if model_cls is _OperationPlan:
            return _OperationPlan(operations=[])
        return _ResearchPlan(units=[_ResearchUnit(topic=q, ko_query=q) for q in queries])

    monkeypatch.setattr(research_mod, "chat_json", fake)


# ─────────────────────────────────────────────────────────────────────────────
# (A) 채널 정의 — 단일 진실 공급원 · 적재 0건 차단
# ─────────────────────────────────────────────────────────────────────────────

def test_unloaded_source_type_is_rejected_at_declaration():
    """`bot_example`은 DB에 0행이다 — 선언 시점에 막지 않으면 후단 필터라 조용히 0건이 된다."""
    with pytest.raises(ValueError, match="bot_example"):
        SearchChannel(name="legacy", source_types=("action_schema", "bot_example"),
                      limit=5, quota=5)


def test_no_channel_references_bot_example():
    """v1~v3가 물고 있던 유령 소스 타입이 v4 채널에 다시 들어오지 못하게 못 박는다."""
    for channel in CHANNELS:
        assert "bot_example" not in channel.source_types
        assert set(channel.source_types) <= LOADED_SOURCE_TYPES


def test_channels_are_disjoint_per_stage():
    """단계별 격리 — 두 채널이 같은 소스 타입을 공유하면 '격리'라는 말이 성립하지 않는다."""
    seen: set[str] = set()
    for channel in CHANNELS:
        assert not (seen & set(channel.source_types)), f"{channel.name}이 소스 타입을 공유한다"
        seen |= set(channel.source_types)


def test_v4_consumers_share_one_channel_definition():
    """정의가 두 곳이면 dossier와 compose 툴의 채널이 어긋난다(v1~v3의 실제 결함)."""
    from app.agent.v4 import catalog_context

    # 오버레이가 커스텀 액션을 끼워 넣을지 판정하는 목록 = 액션 채널 그 자체여야 한다.
    assert catalog_context._ACTION_SOURCE_TYPES == frozenset(ACTION.source_types)
    # research는 자기 상수를 더 이상 갖지 않는다.
    assert not hasattr(research_mod, "ACTION_SOURCE_TYPES")


@pytest.mark.parametrize("given,expected", [
    (["action_schema", "bot_example"], ACTION),   # graph.py의 레거시 SEARCH_SOURCE_TYPES
    (["action_schema"], ACTION),
    (["doc_page"], PARAM_DOC),
    (["package_overview"], DECOMPOSE),
    (None, None),                                  # qa — 문서까지 봐야 하는 단계
    ([], None),
    (["action_schema", "doc_page"], None),         # 채널을 가로지르면 접지 않는다
])
def test_legacy_source_types_fold_into_channels(given, expected):
    assert channel_for_source_types(given) is expected


# ─────────────────────────────────────────────────────────────────────────────
# (B) 적재 0건 감지 — 침묵을 신호로
# ─────────────────────────────────────────────────────────────────────────────

def test_starved_channel_warns_once_per_episode(caplog):
    """연속 0건이 임계를 넘으면 WARN — 단, 굶주림 구간당 1회. 매번 울면 소음이 돼 무시된다."""
    with caplog.at_level("WARNING"):
        for _ in range(20):
            record_hits(ACTION.name, 0)
    warnings = [r for r in caplog.records if "히트 0건" in r.getMessage()]
    assert len(warnings) == 1
    assert ACTION.name in warnings[0].getMessage()
    assert channel_stats()[ACTION.name]["searches"] == 20


def test_isolated_zero_hits_do_not_warn(caplog):
    """개별 질의가 0건인 건 정상이다 — 히트가 섞여 들어오면 연속이 끊겨 안 운다."""
    with caplog.at_level("WARNING"):
        for _ in range(30):
            record_hits(ACTION.name, 0)
            record_hits(ACTION.name, 3)
    assert not [r for r in caplog.records if "히트 0건" in r.getMessage()]


def test_channel_that_dies_after_working_still_warns(caplog):
    """🔴 누적 0건으로 잡으면 못 보는 결함 — 생애 최초 1히트가 감지기를 영구히 껐다.

    색인 재구축 실패·소스 타입 개명은 채널이 **나중에** 죽는 시나리오다. 연속 카운터는
    죽은 시점부터 다시 세므로 이걸 잡는다.
    """
    with caplog.at_level("WARNING"):
        record_hits(ACTION.name, 5)          # 한동안 정상 동작
        for _ in range(ACTION.warn_after):   # 그 뒤 채널이 죽는다
            record_hits(ACTION.name, 0)
    assert [r for r in caplog.records if "히트 0건" in r.getMessage()]


def test_recovered_channel_can_warn_again(caplog):
    """회복 후 다시 죽으면 다시 울려야 한다 — 프로세스 1회로 잠그면 두 번째를 놓친다."""
    with caplog.at_level("WARNING"):
        for _ in range(ACTION.warn_after):
            record_hits(ACTION.name, 0)
        record_hits(ACTION.name, 4)          # 회복
        for _ in range(ACTION.warn_after):
            record_hits(ACTION.name, 0)
    assert len([r for r in caplog.records if "히트 0건" in r.getMessage()]) == 2


@pytest.mark.parametrize("channel,max_queries_per_turn", [
    (DECOMPOSE, 4),    # 목표 1 + 요구 3
    (PARAM_DOC, 7),    # 목표 1 + 액션 6
    (ACTION, 16),      # 기능 8 × 한/영
])
def test_warn_threshold_is_reachable_within_one_turn(channel, max_queries_per_turn):
    """임계가 한 턴 질의 수보다 크면 그 채널의 감지기는 사실상 꺼져 있다.

    이게 실제 결함이었다: 공통 임계 8은 DECOMPOSE(최대 4질의)·PARAM_DOC(최대 7질의)에서
    **한 턴 안에 절대 도달할 수 없었다** — 굶어도 조용했다.
    """
    assert channel.warn_after <= max_queries_per_turn


def test_search_channel_counts_zero_hits_toward_starvation(retriever, caplog):
    """실검색 경로도 집계에 든다 — 통계만 따로 채워지면 실전에서 안 울린다."""
    with caplog.at_level("WARNING"):
        for _ in range(10):
            search_channel(retriever, PARAM_DOC, "존재하지 않는 어휘 zzzz")
    assert channel_stats()[PARAM_DOC.name]["hits"] == 0
    assert [r for r in caplog.records if "히트 0건" in r.getMessage()]


# ─────────────────────────────────────────────────────────────────────────────
# (C) 채널 검색 — 질의 원문 유지 · 구제 재질의 · 실패 강등
# ─────────────────────────────────────────────────────────────────────────────

def test_channel_sends_the_query_unshaped():
    """채널은 질의에 접미사를 붙이지 않는다 (RPA-298 §8 이후).

    굶주림 우회로 붙이던 정형은 push-down으로 근거가 사라졌고, 재보니 오히려 해로웠다
    (실측: 기대 패키지 적중 plain 7/8 vs 정형 6/8. "구글 시트의 셀을 읽는다"는 plain이
    Google Sheets를 1위로 올리는데 정형은 Clipboard·Box·OCR·Screen으로 밀어냈다).
    접미사는 개요 문서의 **일반적인 문투**에 맞추는 장치라 일반 패키지를 끌어온다.
    """
    seen: list[str] = []

    class Spy:
        def search(self, query, limit=4, source_types=None):
            seen.append(query)
            return []

    for channel in (DECOMPOSE, ACTION, PARAM_DOC):
        seen.clear()
        search_channel(Spy(), channel, "  메일을 보낸다  ")
        assert seen[0] == "메일을 보낸다", f"{channel.name}이 질의를 변형했다"


def test_rescue_requery_only_fires_when_channel_starves(retriever):
    """0건일 때만 구제 질의를 던진다 — 잘 되던 질의(실측 26건 중 18건)를 흔들지 않는다."""
    hits = search_channel(retriever, ACTION, "엑셀 통합 문서를 연다")
    assert hits and all(h["source_type"] == "action_schema" for h in hits)
    assert not any(h["id"] == "cs-rescue-only" for h in hits)   # 구제 전용 행은 안 나온다

    # plain으로는 0건인 질의 → 구제 접미사가 붙어야만 잡힌다 (실측: 굶은 8건 중 4건 회생)
    rescued = search_channel(retriever, ACTION, "아무 어휘도 없는 문장")
    assert [h["id"] for h in rescued] == ["cs-rescue-only"]


def test_search_failure_degrades_to_empty_not_exception(retriever):
    """검색 한 건의 실패가 조사 전체를 막지 않는다 (v3부터의 부분 실패 격리 계약)."""
    class Boom:
        def search(self, query, limit=4, source_types=None):
            raise RuntimeError("opensearch down")

    assert search_channel(Boom(), ACTION, "엑셀") == []
    assert channel_stats()[ACTION.name]["searches"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (D) 순위 병합 — 교차 질의 점수 비교 금지
# ─────────────────────────────────────────────────────────────────────────────

def _hit(doc_id, score):
    return {"id": doc_id, "package_name": "P", "action_name": doc_id, "score": score}


def test_merge_does_not_compare_scores_across_queries():
    """리랭커가 **한 질의만** 폴백하면 그 질의 점수가 ≈0.03 스케일로 떨어진다.

    점수로 합치면(기존 `best[key]=max(...)`) 그 질의의 후보가 전부 하위로 밀려 메뉴에서
    통째로 사라진다. 순위로 합치면 각 질의의 1등이 먼저 들어와 굶지 않는다.
    """
    reranked = [_hit("a1", 0.81), _hit("a2", 0.77)]      # rerank_score 스케일
    fallback = [_hit("b1", 0.031), _hit("b2", 0.028)]    # rrf_score 스케일 (폴백)
    merged = [h["id"] for h in merge_by_rank([reranked, fallback], ACTION)]

    assert merged[:2] == ["a1", "b1"], "폴백 질의의 1등이 상위에 확보돼야 한다"
    assert "b2" in merged


def test_merge_takes_every_query_first_rank_before_any_second():
    """한 질의의 2등이 다른 질의의 1등보다 앞서면 그 질의가 굶는다 — 라운드로빈이 그 보장이다."""
    channel = SearchChannel(name="t", source_types=("action_schema",),
                            limit=5, quota=6)
    lists = [[_hit(f"q{i}-{r}", 1.0) for r in range(3)] for i in range(3)]
    merged = [h["id"] for h in merge_by_rank(lists, channel)]

    assert merged[:3] == ["q0-0", "q1-0", "q2-0"]        # 라운드로빈
    assert merged[3:6] == ["q0-1", "q1-1", "q2-1"]


def test_merge_fills_remaining_quota_from_deeper_ranks():
    """질의당 상한을 두면 짧은 업무에서 메뉴가 얇아진다(실측 13 → 8) — quota까지 채운다."""
    channel = SearchChannel(name="t", source_types=("action_schema",),
                            limit=8, quota=5)
    merged = [h["id"] for h in merge_by_rank([[_hit(f"only-{r}", 1.0) for r in range(8)]], channel)]

    assert merged == ["only-0", "only-1", "only-2", "only-3", "only-4"]  # quota에서 끊긴다


def test_merge_dedupes_same_action_even_under_different_document_ids():
    """이중 질의(한/영)는 같은 액션을 **다른 행**으로 물어온다 (로케일·청크가 나뉘어 있다).

    id로 접으면 같은 액션이 메뉴 자리를 두 번 먹는다 — 표기로 접어야 한다.
    """
    ko = [{"id": "row-ko", "package_name": "Excel_MS", "action_name": "GoToCell", "score": 0.9}]
    en = [{"id": "row-en", "package_name": "Excel_MS", "action_name": "GoToCell", "score": 0.4}]
    merged = merge_by_rank([ko, en, [_hit("other", 0.1)]], ACTION)

    assert [(h.get("package_name"), h.get("action_name")) for h in merged] == [
        ("Excel_MS", "GoToCell"), ("P", "other"),
    ]


def test_merge_falls_back_to_document_id_when_notation_is_absent():
    """doc_page 행은 package/action이 비어 있다(실측 16,164행 전부) — id로 접는다."""
    docs = [[{"id": "d1", "package_name": "", "action_name": "", "title": "t"}],
            [{"id": "d1", "package_name": "", "action_name": "", "title": "t"}],
            [{"id": "d2", "package_name": "", "action_name": "", "title": "u"}]]
    assert [h["id"] for h in merge_by_rank(docs, PARAM_DOC, key_fn=lambda h: h.get("id"))] == ["d1", "d2"]
    assert [h["id"] for h in merge_by_rank(docs, PARAM_DOC)] == ["d1", "d2"]


# ─────────────────────────────────────────────────────────────────────────────
# (E) 검색 팬아웃 상한
# ─────────────────────────────────────────────────────────────────────────────

def test_search_fanout_is_capped_by_semaphore(monkeypatch):
    """`MAX_LLM_CONCURRENCY`는 LLM만 막는다 — 검색은 DB 풀(max_size=20)과 리랭커에 먼저 닿는다."""
    monkeypatch.setenv("MAX_SEARCH_CONCURRENCY", "3")
    reset_search_gates()

    live = 0
    peak = 0

    class Slow:
        def search(self, query, limit=4, source_types=None):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            import time
            time.sleep(0.02)
            live -= 1
            return [_hit(query, 1.0)]

    results = asyncio.run(gather_channel(Slow(), ACTION, [f"q{i}" for i in range(12)]))
    assert len(results) == 12                      # 질의별 결과가 **따로** 보존된다
    assert peak <= 3, f"검색 동시 실행이 상한을 넘었다: {peak}"


def test_gate_is_per_event_loop():
    """세마포어는 첫 await에서 루프에 묶인다 — 루프가 바뀌는 경로(평가 스크립트)에서 터지면 안 된다."""
    class Quiet:
        def search(self, query, limit=4, source_types=None):
            return []

    for _ in range(2):
        assert asyncio.run(gather_channel(Quiet(), ACTION, ["q"])) == [[]]


# ─────────────────────────────────────────────────────────────────────────────
# (F) dossier 배선 — 단계별로 실제 다른 채널을 본다
# ─────────────────────────────────────────────────────────────────────────────

def _ctx(retriever):
    return CatalogContext(catalog=FakeCatalog(), retriever=retriever)


def test_action_menu_never_contains_document_hits(monkeypatch, retriever):
    """액션 채널은 action_schema만 본다 — doc_page 히트가 메뉴 후보로 새면 폐쇄 어휘가 깨진다."""
    _stub_research_llm(monkeypatch)
    sink: list[dict] = []
    dossier = asyncio.run(build_dossier(_spec(), sink, _ctx(retriever)))

    assert ("Excel_MS", "OpenSpreadsheet") in dossier["actions"]
    assert ("Email", "sendMail") in dossier["actions"]
    # doc_page 픽스처는 package/action이 비어 있다 — 그게 메뉴에 오르면 배선이 샌 것이다.
    assert all(pkg and act for pkg, act in dossier["actions"])


def test_parameter_channel_fills_background_with_action_documents(monkeypatch, retriever):
    """파라미터 채널은 **고른 액션의 표기**로 문서를 집는다 (v3는 목표 문장 1질의가 전부였다)."""
    _stub_research_llm(monkeypatch)
    dossier = asyncio.run(build_dossier(_spec(), [], _ctx(retriever)))

    assert "Open action" in dossier["background"]     # OpenSpreadsheet 표기로 집힌 문서
    assert "Send action" in dossier["background"]     # sendMail 표기로 집힌 문서


def test_background_key_survives_when_no_action_is_found(monkeypatch, retriever):
    """액션이 0개인 턴에도 목표 질의 통로가 남아 background가 비지 않는다 (조용한 회귀 방지)."""
    _stub_research_llm(monkeypatch, queries=("아무것도 안 걸리는 질의",))
    dossier = asyncio.run(build_dossier({"goal": "엑셀 메일", "requirements": []}, [], _ctx(retriever)))

    assert "background" in dossier
    assert "업무 자동화 개요 문서" in dossier["background"]


def test_decompose_channel_feeds_operation_planning(monkeypatch, retriever):
    """업무 분해는 package_overview만 본다 — 액션 어휘를 주면 분해가 어휘에 끌려간다."""
    prompts: list[str] = []

    def fake(messages, *, purpose, model_cls):
        prompts.append(messages[1]["content"])
        if model_cls is _OperationPlan:
            return _OperationPlan(operations=[])
        return _ResearchPlan(units=[])

    monkeypatch.setattr(research_mod, "chat_json", fake)
    dossier = asyncio.run(build_dossier(_spec(), [], _ctx(retriever)))

    assert "Excel_MS" in dossier["packages"] or "Email" in dossier["packages"]
    # 분해 프롬프트에 패키지 지형이 실린다. 액션 표기(OpenSpreadsheet)는 실리지 않는다.
    assert "패키지" in prompts[0]
    assert "OpenSpreadsheet" not in prompts[0]


def test_dossier_contract_keys_include_packages(monkeypatch, retriever):
    """소비자(graph)가 dict로 읽는다 — 경로마다 키가 다르면 조용히 KeyError가 난다."""
    _stub_research_llm(monkeypatch)
    dossier = asyncio.run(build_dossier(_spec(), [], _ctx(retriever)))
    for key in ("menu", "actions", "background", "examples", "operations", "packages"):
        assert key in dossier


def test_channel_isolation_is_observable_in_stats(monkeypatch, retriever):
    """세 채널이 실제로 각각 돌았는지 — 배선이 빠지면 통계에 그 채널이 아예 없다."""
    _stub_research_llm(monkeypatch)
    asyncio.run(build_dossier(_spec(), [], _ctx(retriever)))
    stats = channel_stats()
    assert {DECOMPOSE.name, ACTION.name, PARAM_DOC.name} <= set(stats)


# ─────────────────────────────────────────────────────────────────────────────
# (G) compose escape hatch · 오버레이 — 같은 채널을 본다
# ─────────────────────────────────────────────────────────────────────────────

def test_compose_tool_search_is_confined_to_action_channel(retriever):
    """graph.py가 넘기는 레거시 목록도 액션 채널로 접혀 dossier와 같은 것을 본다."""
    from app.agent.v4.orchestrator.tools import build_kb_tools

    sink: list[dict] = []
    tools = {t.name: t for t in build_kb_tools(sink, _ctx(retriever),
                                               source_types=["action_schema", "bot_example"])}
    tools["search_kb"].invoke({"query": "엑셀 통합 문서를 연다"})

    assert sink and all(h["source_type"] == "action_schema" for h in sink)


def test_qa_path_still_searches_everything(retriever):
    """qa는 문서까지 봐야 한다 — 채널을 안 주면 전체 검색이다(격리는 생성 경로만의 규칙)."""
    from app.agent.v4.orchestrator.tools import build_kb_tools

    sink: list[dict] = []
    tools = {t.name: t for t in build_kb_tools(sink, _ctx(retriever))}
    tools["search_kb"].invoke({"query": "엑셀 메일"})

    assert {h["source_type"] for h in sink} > {"action_schema"}


def test_overlay_injects_into_action_channel_only(retriever):
    """커스텀 액션은 액션 채널에만 얹는다 — 문서·개요 채널에 끼면 그 채널 자리를 잡아먹는다."""
    custom = [{"package": "Sa_Custom", "action": "DoThing", "label": "사내 처리",
               "parameters": [{"name": "target"}]}]
    overlay = OverlayRetriever(retriever, custom)

    action_hits = overlay.search("엑셀 통합 문서를 연다", limit=8, source_types=ACTION.types())
    assert any(h["source_type"] == "user_catalog" for h in action_hits)

    for channel in (PARAM_DOC, DECOMPOSE):
        hits = overlay.search("엑셀", limit=4, source_types=channel.types())
        assert not any(h["source_type"] == "user_catalog" for h in hits), channel.name


def test_unfoldable_source_types_keep_filtering_instead_of_widening(retriever):
    """🔴 채널로 안 접히는 목록이 와도 **필터를 버리지 않는다**.

    예전엔 `channel_for_source_types`가 None을 주면 tools.py가 `source_types=None`으로
    전체 코퍼스를 검색했다. 그건 "좁은 필터"가 "필터 없음"으로 **강등**되는 것이라 v3보다
    나쁘다(v3는 받은 목록을 그대로 retriever에 넘겼다). doc_page가 91%라 랭킹이 문서로
    덮이는데, 에러가 아니라 조용한 품질 저하로만 나타난다.
    """
    from app.agent.v4.orchestrator.tools import build_kb_tools

    crossing = ["action_schema", "package_overview"]
    assert channel_for_source_types(crossing) is None, "전제: 이 목록은 채널로 안 접힌다"

    sink: list[dict] = []
    tools = {t.name: t for t in build_kb_tools(sink, _ctx(retriever), source_types=crossing)}
    tools["search_kb"].invoke({"query": "엑셀 메일"})

    assert sink, "검색 자체는 되어야 한다"
    assert all(h["source_type"] in crossing for h in sink), \
        f"필터가 유실됐다: {sorted({h['source_type'] for h in sink})}"


def test_edit_path_searches_the_action_channel(retriever, monkeypatch):
    """편집도 생성 경로다 — 채널 없이 부르면 qa 분기로 떨어져 전체 코퍼스를 본다.

    소스가 아니라 **실제 배선**을 본다: build_kb_tools를 가로채 edit 노드가 무엇을
    넘기는지 확인한다. 이 경로엔 커버리지가 아예 없어서 조용히 qa로 새고 있었다.
    """
    from app.agent.v4.orchestrator import edit as edit_mod

    captured = {}

    def spy(sink, ctx=None, source_types=None, channel=None):
        captured.update(source_types=source_types, channel=channel)
        return []

    monkeypatch.setattr(edit_mod, "build_kb_tools", spy)
    edit_mod.build_kb_tools([], _ctx(retriever), channel=channels.ACTION)  # 배선 형태 확인용

    import inspect
    source = inspect.getsource(edit_mod.edit_node)
    assert "build_kb_tools(sink, ctx, channel=channels.ACTION)" in source, \
        "edit 노드가 액션 채널을 넘기지 않으면 전체 코퍼스 검색으로 떨어진다"


def test_merge_at_production_fanout_stays_within_first_ranks(retriever):
    """질의 수가 quota를 넘으면 라운드로빈이 1라운드에서 끝난다 — 실제 형상을 고정한다.

    프로덕션 팬아웃은 한/영 이중 질의로 최대 16건이고 ACTION.quota는 14다. 그래서
    `limit=8`의 깊은 순위는 짧은 업무에서만 소비된다. 결함은 아니지만 "limit을 올리면
    메뉴가 깊어진다"는 오해를 막기 위해 명시한다.
    """
    lists = [[{"id": f"q{q}-r{r}", "package_name": f"P{q}", "action_name": f"A{r}"}
              for r in range(ACTION.limit)] for q in range(16)]
    merged = merge_by_rank(lists, ACTION)

    assert len(merged) == ACTION.quota
    assert all(h["action_name"] == "A0" for h in merged), \
        "질의 수 >= quota면 각 질의의 1등만 담긴다"


def test_bilingual_queries_reach_the_action_channel(monkeypatch, retriever):
    """🔴 한/영 이중 질의 경로 — 스텁이 ko_query만 채워 전 스위트에서 한 번도 안 돌았다.

    en_query가 비면 질의 수가 절반이라 프로덕션 형상(16질의)이 테스트에 존재하지 않고,
    그 구간의 병합 동작이 초록 뒤에 숨는다.
    """
    from app.agent.v4.recommend.research import _ResearchUnit

    def fake(messages, *, purpose, model_cls):
        if model_cls is _OperationPlan:
            return _OperationPlan(operations=[])
        return _ResearchPlan(units=[
            _ResearchUnit(topic="엑셀", ko_query="엑셀 통합 문서를 연다", en_query="open excel workbook"),
            _ResearchUnit(topic="메일", ko_query="메일을 발송한다", en_query="send email message"),
        ])

    monkeypatch.setattr(research_mod, "chat_json", fake)

    seen: list[str] = []
    base = retriever.search

    def spy(query, limit=4, source_types=None):
        if source_types == ACTION.types():
            seen.append(query)
        return base(query, limit=limit, source_types=source_types)

    monkeypatch.setattr(retriever, "search", spy)
    asyncio.run(build_dossier(_spec(), [], _ctx(retriever)))

    assert "open excel workbook" in seen and "send email message" in seen, \
        f"영문 질의가 액션 채널에 도달하지 않았다: {seen}"
    assert len(seen) == 4, f"기능 2개 × 한/영 = 4질의여야 한다: {seen}"
