"""done.data의 실제 실행 버전 공개 계약 (RPA-184).

백엔드는 요청값(`agent_version`)이나 서버 기본값(env `AGENT_VERSION`)만으로는 **무엇이 돌았는지**
확정할 수 없다 — 둘 다 "무엇을 원했나"이지 "무엇이 돌았나"가 아니다. 그래서 보증 영수증이
`resolved_agent_version` 하나 때문에 `incomplete`로 남았다(실측). 이 파일은 그 공백을 메우는
공개 계약을 고정한다.

버전 구현(v1/v2/v3)은 **띄우지 않는다** — 계약을 지키는 주체가 디스패처이므로, 페이크 구현을
꽂아 "디스패처가 새기는가"만 본다. 실제 vN을 태우면 LLM·RAG가 딸려와 계약 검증이 인프라
테스트가 되고, 정작 새로 드롭될 `v4/`는 검증 대상에서 빠진다.

⚠️ 비동기 구동은 `asyncio.run()`으로 직접 한다 — 이 리포엔 async 테스트가 없고
pytest-asyncio도 안 깔려 있다. 계약 테스트 하나 때문에 테스트 관례를 새로 들이지 않는다.
"""

import asyncio

import pytest

import app.agent as agent_pkg
from app.agent import RESOLVED_VERSION_FIELD
from app.agent.registry import _discover, default_version, resolve_version_name
from app.schemas import ProgressEvent


class FakeImpl:
    """버전 구현 스텁 — 디스패처가 넘기는 이벤트만 그대로 흘린다."""

    def __init__(self, *events: ProgressEvent):
        self.events = list(events)
        self.calls: list[tuple[str, dict]] = []
        self.imported: list[str] = []  # 디스패처가 어느 이름으로 import했나

    async def stream_agent_turn(self, message: str, context: dict):
        self.calls.append((message, context))
        for event in self.events:
            yield event


def _done(**data) -> ProgressEvent:
    return ProgressEvent(event="done", stage="agent", data={"type": "answer", "answer": "", **data})


def _run(monkeypatch, impl: FakeImpl, context: dict) -> list[ProgressEvent]:
    """디스패처를 태우고 이벤트를 모은다. `resolve_version_name`은 **진짜**를 쓴다.

    이름 해석(기본값 결정·미지 버전 거부)이 계약의 절반이라, 거기까지 스텁하면 남는 게 없다.
    갈아끼우는 건 import 쪽뿐이다 — 실제 vN 스택을 안 띄우려는 것이지 해석을 우회하려는 게 아니다.

    ⚠️ **import에 넘어간 이름을 버리지 않고 기록한다** (Qodo #475). 스텁이 `lambda name: impl`로
    이름을 무시하면, 디스패처가 A를 import하고 B를 새겨도 테스트가 통과한다 — 이 계약의 핵심이
    "돌아간 버전 = 보고된 버전"인데 그 등식이 검증에서 빠지는 것이다. 그래서 done을 낸 모든
    실행에 대해 **import한 이름과 새긴 이름이 같은지**를 여기서 못 박는다.
    """
    monkeypatch.setattr(
        agent_pkg, "import_version", lambda name: (impl.imported.append(name), impl)[1]
    )

    async def collect() -> list[ProgressEvent]:
        return [e async for e in agent_pkg.stream_agent_turn("안녕", context)]

    events = asyncio.run(collect())

    stamped = [e.data[RESOLVED_VERSION_FIELD] for e in events
               if e.event == "done" and RESOLVED_VERSION_FIELD in (e.data or {})]
    for name in stamped:
        assert impl.imported == [name], (
            f"import한 버전({impl.imported})과 done에 새긴 버전({name})이 다르다"
        )
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 버전 선택 규칙별 resolved 값
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("version", _discover())
def test_명시_버전은_그대로_보고된다(monkeypatch, version):
    """요청이 버전을 명시하면 resolved가 그 값이다 — 발견된 **모든** 버전에 대해.

    v1·v2를 하드코딩하면 새 버전이 계약 밖에 남는다(registry 자동탐색과 같은 이유).

    **실제로 그 버전을 import했는지**까지 본다 — 새긴 이름만 맞고 다른 버전이 돌면 기록이
    거짓말을 하는 것이고, 그게 이 계약이 막으려는 바로 그 실패다.
    """
    impl = FakeImpl(_done())
    events = _run(monkeypatch, impl, {"agent_version": version})
    assert events[-1].data[RESOLVED_VERSION_FIELD] == version
    assert impl.imported == [version]


def test_요청값이_없어도_비어_있지_않다(monkeypatch):
    """서버 기본으로 돌아도 resolved는 채워진다 — RPA-184 완료 조건."""
    events = _run(monkeypatch, FakeImpl(_done()), {})
    resolved = events[-1].data[RESOLVED_VERSION_FIELD]
    assert resolved == default_version()
    assert resolved  # null도 빈 문자열도 아니다


def test_agent_version_키_자체가_없어도_동작한다(monkeypatch):
    """context에 키가 아예 없는 구형 호출도 기본 버전으로 처리되고 resolved가 남는다."""
    events = _run(monkeypatch, FakeImpl(_done()), {"solution": "a360"})
    assert events[-1].data[RESOLVED_VERSION_FIELD] == default_version()


def test_서버_기본값이_env를_따르면_resolved도_따라간다(monkeypatch):
    """env로 기본을 바꾸면 보고되는 실행 버전도 같이 바뀐다 — 둘이 어긋나면 기록이 거짓말한다."""
    monkeypatch.setenv("AGENT_VERSION", "v1")
    events = _run(monkeypatch, FakeImpl(_done()), {"agent_version": None})
    assert events[-1].data[RESOLVED_VERSION_FIELD] == "v1"


def test_지원하지_않는_버전은_대체되지_않고_거부된다(monkeypatch):
    """미지 버전은 ValueError — 다른 버전으로 조용히 갈아끼우지 않는다 (D-24).

    '조용한 대체'가 가장 나쁜 실패다: 사용자는 v99를 받은 줄 알고 기록에는 기본 버전이 남는다.
    """
    impl = FakeImpl(_done())
    monkeypatch.setattr(
        agent_pkg, "import_version", lambda name: (impl.imported.append(name), impl)[1]
    )

    async def drain() -> None:
        async for _ in agent_pkg.stream_agent_turn("안녕", {"agent_version": "v99"}):
            pass

    with pytest.raises(ValueError, match="v99"):
        asyncio.run(drain())
    assert impl.calls == []  # 어떤 버전도 실행되지 않았다
    # import까지 가지 않는다 — import_version은 검증하지 않으므로 여기서 막혀야 한다.
    assert impl.imported == []


def test_env_미지값_폴백은_이름_해석에서_드러난다(monkeypatch):
    """env 오설정 폴백은 부팅을 죽이지 않되, resolved가 **실제로 돈 버전**을 말한다.

    요청 경로(위 테스트)와 달리 env 경로는 폴백이 허용된 결정이다
    (test_default_version_falls_back_for_unknown_env). 그 결정을 유지하면서도 "무엇이
    돌았나"는 정확해야 한다 — 그래야 대체 사실이 기록에서 보인다.
    """
    monkeypatch.setenv("AGENT_VERSION", "v99")
    assert resolve_version_name(None) == default_version() != "v99"


# ─────────────────────────────────────────────────────────────────────────────
# 새기는 방식
# ─────────────────────────────────────────────────────────────────────────────


def test_기존_done_필드를_보존한다(monkeypatch):
    """판별 유니온의 기존 키를 하나도 잃지 않는다 — 백엔드 저장 분기가 이 위에 서 있다."""
    impl = FakeImpl(_done(type="recommendation", updated_recommendation={"steps": []},
                          change_summary="첫 산출", sources=[{"title": "t"}]))
    data = _run(monkeypatch, impl, {"agent_version": "v1"})[-1].data
    assert data["type"] == "recommendation"
    assert data["updated_recommendation"] == {"steps": []}
    assert data["change_summary"] == "첫 산출"
    assert data["sources"] == [{"title": "t"}]
    assert data[RESOLVED_VERSION_FIELD] == "v1"


def test_구현의_자기신고를_디스패처가_덮어쓴다(monkeypatch):
    """버전이 자기 이름을 적어 보내도 디스패처 값이 이긴다 — provenance의 권위는 여기다."""
    impl = FakeImpl(_done(**{RESOLVED_VERSION_FIELD: "v99"}))
    events = _run(monkeypatch, impl, {"agent_version": "v1"})
    assert events[-1].data[RESOLVED_VERSION_FIELD] == "v1"


def test_data가_없는_done에도_새긴다(monkeypatch):
    """data는 계약상 nullable — 그래도 실행 버전은 남아야 한다(관측에 구멍을 만들지 않는다)."""
    impl = FakeImpl(ProgressEvent(event="done", stage="agent"))
    events = _run(monkeypatch, impl, {"agent_version": "v2"})
    assert events[-1].data == {RESOLVED_VERSION_FIELD: "v2"}


def test_done이_아닌_이벤트는_건드리지_않는다(monkeypatch):
    """stage/partial/token/error는 그대로 통과 — 프론트가 보는 스트림이 바뀌면 안 된다."""
    stage = ProgressEvent(event="stage", stage="routing", message="요청 분석 중")
    token = ProgressEvent(event="token", message="안")
    error = ProgressEvent(event="error", message="실패")
    events = _run(monkeypatch, FakeImpl(stage, token, error), {"agent_version": "v1"})
    assert events == [stage, token, error]
    assert all(RESOLVED_VERSION_FIELD not in (e.data or {}) for e in events)


def test_구현이_넘긴_data를_제자리에서_고치지_않는다(monkeypatch):
    """새김은 복사본에 한다 — done data는 그래프 상태와 같은 객체일 수 있다.

    제자리에서 고치면 에이전트 내부 상태를 오염시킨다.
    """
    original = {"type": "answer", "answer": ""}
    impl = FakeImpl(ProgressEvent(event="done", stage="agent", data=original))
    _run(monkeypatch, impl, {"agent_version": "v1"})
    assert original == {"type": "answer", "answer": ""}


# ─────────────────────────────────────────────────────────────────────────────
# operation 무관 — 자동 compact도 실제 실행 버전을 남긴다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("operation", ["chat", "compact", "fill_cards"])
def test_operation과_무관하게_남는다(monkeypatch, operation):
    """자동 compact는 본 처리와 같은 requested 버전으로 돌고, **각각** 자기 resolved를 낸다.

    백엔드는 compact 턴에도 사용자가 고른 버전을 넘긴다(버전 혼선 방지). 그 이행 여부는 두
    기록의 resolved를 비교해야 확인되므로, compact 턴에서 필드가 빠지면 그 확인이 불가능해진다.
    """
    impl = FakeImpl(_done(type="compact", compact={"sections": []}))
    events = _run(monkeypatch, impl, {"agent_version": "v3", "operation": operation})
    assert events[-1].data[RESOLVED_VERSION_FIELD] == "v3"
    assert impl.calls[0][1]["operation"] == operation  # context는 그대로 전달된다


# ─────────────────────────────────────────────────────────────────────────────
# 소비처와의 결속
# ─────────────────────────────────────────────────────────────────────────────


def test_필드명이_보증_증거_계약과_일치한다():
    """에이전트가 내는 이름과 보증 레이어가 기대하는 이름이 같아야 한다.

    이름이 어긋나면 아무도 안 터지고 영수증만 조용히 `incomplete`로 남는다 — 실측으로 이미
    한 번 겪은 실패 양식이라, 문자열을 양쪽에서 각자 적지 않고 여기서 묶는다.
    """
    from app.services.assurance_evidence import OUTPUT_EXPECTED_AGENT

    assert RESOLVED_VERSION_FIELD in OUTPUT_EXPECTED_AGENT
