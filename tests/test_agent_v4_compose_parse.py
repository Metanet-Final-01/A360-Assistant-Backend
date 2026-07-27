# -*- coding: utf-8 -*-
"""compose 후보의 JSON 파싱 실패 — 원인을 가르고 원인별로 다시 묻는다 (RPA-298 항목 B).

## 무엇을 막는가 (실측, 2026-07-27)

    후보 A ✅  후보 B ❌ JSON 파싱 실패(char 10445)  후보 C ❌ JSON 파싱 실패(char 12286)

3후보 중 2개가 탈락해 **심판이 후보 하나만 보고 승자를 골랐다.** 다중 후보 경쟁이 그 턴에는
존재하지 않았다는 뜻이고, `attach_confidence`의 agreement 항(후보 2개 이상일 때만)도 통째로
꺼졌다. 그런데 로그에는 "파싱 실패"만 남아 **잘림인지 문법 오류인지 알 수 없었다.**

두 원인은 처방이 정반대다:

  - 잘림  → 부피를 줄여 다시 쓰게 한다. 깨진 본문은 설계 뼈대로 갈아끼운다.
  - 문법  → **깨진 본문을 남긴다.** 모델이 자기가 쓴 걸 봐야 그 자리를 고친다.

전부 잘림으로 취급해 본문을 지우면 문법 오류에서는 **현행보다 나빠진다** — 그래서 분류가
이 항목의 핵심이고, 이 파일은 분류 경계를 하나씩 못 박는다. LLM은 한 번도 부르지 않는다.
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app.agent.v4.recommend import graph as g


# ── 분류 ─────────────────────────────────────────────────────────────────────

def test_finish_reason이_length면_잘림으로_분류된다():
    """괄호 균형만 보면 상한 절단을 놓친다 — 우연히 균형이 맞은 지점에서 끊길 수 있다."""
    assert g._classify_parse_failure('{"steps": []}', "length") == g.PARSE_TRUNCATED


def test_이스케이프_안된_따옴표는_문법오류로_분류된다():
    """🔴 이 레포가 "잦다"고 기록해 둔 실패(edit_ops 모듈 독스트링) — 잘림으로 오분류하면
    깨진 본문을 지우고 무관한 '부피를 줄여라'를 보내 **현행보다 나빠진다**.

    미이스케이프 `"` 하나가 그 뒤 전체를 문자열 안으로 뒤집으므로 괄호는 미닫힘으로 보인다.
    문자열 안에서 끝났다는 신호가 둘을 가른다.
    """
    broken = '{"steps": [{"label": "매출" 시트를 연다", "actions": []}]}'
    assert g._classify_parse_failure(broken, "stop") == g.PARSE_SYNTAX


def test_값에_중괄호가_있어도_잘림_분류가_흔들리지_않는다():
    """`rfind("}")`나 예외 문구 매칭으로 되돌아가는 리팩터를 막는다 — 값 안의 중괄호가
    문자열 한가운데를 문서 끝으로 잡게 한다."""
    text = '{"steps": [{"label": "치환 {템플릿} 처리", "actions": [{"package": "String"'
    assert g._classify_parse_failure(text, "stop") == g.PARSE_TRUNCATED


@pytest.mark.parametrize("text", [
    '{"steps": [] "notes": null}',    # 쉼표 누락
    "{'steps': []}",                  # 작은따옴표
    '{"steps": [], "notes": None}',   # 파이썬 리터럴
])
def test_진짜_문법오류는_잘림으로_분류되지_않는다(text):
    """전부 잘림 취급하면 모델이 고칠 근거(자기가 쓴 출력)를 뺏긴다."""
    assert g._classify_parse_failure(text, "stop") == g.PARSE_SYNTAX


@pytest.mark.parametrize("finish,expected", [("length", g.PARSE_TRUNCATED), ("stop", g.PARSE_NO_JSON)])
def test_본문이_비면_finish_reason으로_갈린다(finish, expected):
    """추론이 예산을 다 먹어 본문이 빈 경우를 "모델이 답을 안 했다"로 오진하지 않는다."""
    assert g._classify_parse_failure("   ", finish) == expected


def test_모양이_다르면_shape다():
    """파싱은 됐는데 steps가 없다 — 부피가 아니라 지시 이해의 문제라 처방이 다르다."""
    with pytest.raises(g.ComposeParseError) as e:
        g._parse_flow('{"flow": []}')
    assert e.value.kind == g.PARSE_SHAPE


def test_ComposeParseError는_ValueError다():
    """🔴 호출부의 `except ValueError`를 좁히면 `_coerce_flow`가 내는 ValueError가
    asyncio.gather 밖으로 전파돼 **후보 하나의 실패가 턴 전체를 죽인다**(부분 실패 격리 붕괴)."""
    assert issubclass(g.ComposeParseError, ValueError)


# ── 설계 뼈대 ────────────────────────────────────────────────────────────────

def test_잘린_출력에서_설계_뼈대를_뽑는다():
    """잘림 재시도에 깨진 12KB 본문 대신 싣는 것. 본문을 통째로 지우면 모델 문맥에
    '그대로 둘 설계'가 없어 "설계는 유지하고 부피만 줄여라"가 무의미해진다."""
    text = ('{"steps":[{"step_id":"step-1","label":"엑셀 열기","actions":'
            '[{"package":"Excel advanced","action":"cloudExcelOpen"')
    skeleton = g._salvage_skeleton(text)

    assert "label=엑셀 열기" in skeleton
    assert "package=Excel advanced" in skeleton and "action=cloudExcelOpen" in skeleton


def test_설계_뼈대에도_상한이_있다():
    """복구본이 원본만큼 길면 절약이 없다."""
    text = "".join(f'"label":"단계{i}",' for i in range(400))
    assert g._salvage_skeleton(text).count("label=") <= g._MAX_SKELETON_ENTRIES


# ── 재시도 문구 ──────────────────────────────────────────────────────────────

def test_잘림_재시도는_설계를_지키라고_말한다():
    """🔴 이게 없으면 모델은 **요구를 지워서** 부피를 맞춘다 — 조용한 절단을 LLM이 대신
    저지르는 꼴이라, 잘림보다 나쁘다(검수가 '누락'으로만 보고 원인을 못 짚는다)."""
    msg = g._compose_retry_message(g.PARSE_TRUNCATED, "…")
    assert "빼서" in msg and "줄이지 마라" in msg


def test_문법오류_재시도는_이스케이프를_짚는다():
    msg = g._compose_retry_message(g.PARSE_SYNTAX, "Expecting ',' delimiter")
    assert "이스케이프" in msg


# ── 호출 배선 ────────────────────────────────────────────────────────────────

class _Rec:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []


class _FakeRunnable:
    """bind/bind_tools/ainvoke만 흉내 내는 대역 — 어떤 옵션으로 불렸는지를 기록한다."""

    def __init__(self, rec: _Rec, bound: dict | None = None, tools: bool = False):
        self._rec, self._bound, self._tools = rec, bound or {}, tools

    def bind(self, **kw):
        return _FakeRunnable(self._rec, {**self._bound, **kw}, self._tools)

    def bind_tools(self, tools):
        return _FakeRunnable(self._rec, self._bound, True)

    async def ainvoke(self, messages, config=None):
        self._rec.calls.append({"bound": dict(self._bound), "tools": self._tools,
                                "messages": list(messages)})
        return self._rec.responses.pop(0)


def _ai(content: str, finish: str = "stop") -> AIMessage:
    return AIMessage(content=content, response_metadata={"finish_reason": finish},
                     usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})


_GOOD = json.dumps({"steps": [{"step_id": "step-1", "label": "본문", "actions": [
    {"package": "String", "action": "assign", "label": "지정",
     "parameters": [{"name": "value", "value": "x"}]}]}]})


@pytest.fixture
def compose(monkeypatch):
    """(_compose_candidate 실행기, 기록기, emit 이벤트) — 툴은 있는 것으로 둔다."""
    events: list[dict] = []
    monkeypatch.setattr(g, "emit", lambda ev: events.append(ev))

    from app.agent.v4.orchestrator import tools as tools_mod
    monkeypatch.setattr(tools_mod, "build_kb_tools", lambda *a, **k: ["kb-tool"])
    monkeypatch.setattr(tools_mod, "execute_tool_calls", lambda *a, **k: [])

    def run(responses, *, document=None):
        rec = _Rec(responses)
        monkeypatch.setattr(g, "_make_llm", lambda: _FakeRunnable(rec))
        out = asyncio.run(g._compose_candidate(
            "B", "persona_ops.md", {"goal": "테스트", "requirements": []},
            {"menu": "- String/assign", "background": "", "examples": ""},
            {}, document, [], asyncio.Semaphore(1), object(),
        ))
        return out, rec, events

    return run


def test_탐색_턴은_툴을_달고_JSON모드를_안_건다(compose):
    """첫 턴은 escape hatch(KB 조회)가 열려 있어야 한다 — 여기에 JSON 강제를 걸면 툴 호출이
    억제돼 표기 환각이 늘고, 그게 R1 → 사전 검증 폐기로 교정 라운드를 태운다.
    툴+JSON mode 병용은 **미확인 조합**이라 프로브 전에는 켜지 않는다."""
    out, rec, _ = compose([_ai(_GOOD)])

    assert out is not None
    (call,) = rec.calls
    assert call["tools"] is True
    assert "response_format" not in call["bound"]
    assert g._JSON_MODE_WITH_TOOLS is False, "프로브 없이 켜지 않는다"


def test_툴이_없으면_첫_턴부터_JSON모드다(compose, monkeypatch):
    """사용자 제공 카탈로그 경로 — 조회할 KB가 없으니 첫 턴이 곧 출력 턴이다."""
    from app.agent.v4.orchestrator import tools as tools_mod
    monkeypatch.setattr(tools_mod, "build_kb_tools", lambda *a, **k: [])

    out, rec, _ = compose([_ai(_GOOD)])
    assert out is not None
    assert rec.calls[0]["bound"]["response_format"] == {"type": "json_object"}


def test_재출력_턴은_툴_없이_JSON모드로_호출된다(compose):
    """재시도의 일은 출력이지 조사가 아니다 — 그리고 출력 턴에서는 문법이 깨질 여지를 줄인다."""
    _, rec, _ = compose([_ai('{"steps"', "length"), _ai(_GOOD)])

    retry = rec.calls[1]
    assert retry["tools"] is False
    assert retry["bound"]["response_format"] == {"type": "json_object"}


def test_사용자_메시지에_json_문자열이_있다(compose):
    """OpenAI json_object 모드는 메시지 안에 'json' 문자열을 요구한다 — 프롬프트를 손대면
    조용히 400이 난다. 무엇이 그 조건을 채우고 있는지를 못 박는다."""
    _, rec, _ = compose([_ai(_GOOD)])
    user = rec.calls[0]["messages"][1].content
    assert "json" in user.lower()


def test_잘림_재시도는_깨진_본문_대신_설계_뼈대를_싣는다(compose):
    """🔴 12KB 재전송(긴 출력 예시 각인)도, 본문 삭제(설계 유실)도 아니다."""
    broken = '{"steps":[{"step_id":"step-1","label":"엑셀 열기","actions":[{"package":"Excel advanced"'
    out, rec, _ = compose([_ai(broken, "length"), _ai(_GOOD)])

    assert out is not None
    retry_msgs = rec.calls[1]["messages"]
    assert broken not in "".join(str(m.content) for m in retry_msgs), "깨진 본문은 안 싣는다"
    assert "label=엑셀 열기" in str(retry_msgs[-2].content), "대신 설계 뼈대가 실린다"


def test_문법오류_재시도는_깨진_본문을_남긴다(compose):
    """반대 방향 회귀 — 모델이 자기가 쓴 걸 봐야 그 자리를 고친다(jsonio.chat_json과 같은 패턴)."""
    broken = '{"steps": [{"label": "매출" 시트", "actions": []}]}'
    out, rec, _ = compose([_ai(broken), _ai(_GOOD)])

    assert out is not None
    assert broken in "".join(str(m.content) for m in rec.calls[1]["messages"])


def test_재시도_턴은_다시_검색하지_않는다(compose):
    """예전 조건(tool_rounds < 2)은 모델이 툴을 한 번도 안 부르면 **재출력 턴까지 툴을 달고**
    나갔다 — 파싱이 깨진 다음에 필요한 것은 조사가 아니라 출력이다."""
    _, rec, _ = compose([_ai('{"steps"', "length"), _ai('{"steps"', "length"), _ai(_GOOD)])
    assert [c["tools"] for c in rec.calls] == [True, False, False]


def test_재시도_예산을_넘으면_탈락한다(compose):
    """가짜 성공 방지 — 무한 재시도로 턴 예산을 먹지 않는다."""
    out, rec, _ = compose([_ai('{"steps"', "length")] * (g._COMPOSE_PARSE_RETRIES + 1))

    assert out is None
    assert len(rec.calls) == g._COMPOSE_PARSE_RETRIES + 1


def test_compose_예산은_툴2회_출력1회_재시도2회를_담는다():
    """재시도를 늘렸는데 왕복 상한이 그대로면 마지막 재시도가 조용히 잘린다."""
    assert g._COMPOSE_MAX_TURNS == g._ESCAPE_HATCH_ROUNDS + 1 + g._COMPOSE_PARSE_RETRIES


def test_파싱_실패가_이벤트로_드러난다(compose):
    """컨테이너 재시작으로 로그가 날아가도 turn_events에는 남아야 한다 — 없으면 다음 사고에서
    또 정황 증거로만 싸운다."""
    _, _, events = compose([_ai('{"steps"', "length"), _ai(_GOOD)])
    (fail,) = [e for e in events if (e.get("data") or {}).get("kind") == g.PARSE_TRUNCATED]

    assert fail["data"]["finish_reason"] == "length"
    assert fail["data"]["content_chars"] > 0
    assert fail["data"]["output_tokens"] == 20


def test_진단_문구가_사용자_메시지에_새지_않는다(compose):
    """stage message는 프론트가 assistantMessage.stages에 쌓아 **표시한다** —
    truncated·no_json 같은 내부 용어가 비전문가에게 나가면 안 된다."""
    _, _, events = compose([_ai('{"steps"', "length"), _ai(_GOOD)])
    for e in events:
        assert g.PARSE_TRUNCATED not in e.get("message", "")
        assert g.PARSE_NO_JSON not in e.get("message", "")


def test_coerce_실패는_후보_1개_탈락으로_끝난다(compose, monkeypatch):
    """🔴 `except ComposeParseError`로 좁히면 여기서 턴 전체가 죽는다."""
    def boom(obj):
        raise ValueError("예상 못 한 모양")

    monkeypatch.setattr(g, "_coerce_flow", boom)
    out, _, _ = compose([_ai(_GOOD)] * (g._COMPOSE_PARSE_RETRIES + 1))
    assert out is None


def test_compose_사용량은_전용_purpose로_기록된다():
    """"turn_generate"는 spec 빌더·심판·surgeon(라운드당 1회, 최대 8회)이 함께 쓴다 —
    surgeon이 표본을 지배해 compose 실패의 상관을 실측으로 못 뽑는다."""
    assert g._COMPOSE_PURPOSE != "turn_generate"
