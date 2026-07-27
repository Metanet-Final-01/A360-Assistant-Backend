# -*- coding: utf-8 -*-
"""봇 저장 메타 (설계 제약 #15 — 완성도 범위의 마지막 한 칸).

완성도 범위 4종 중 변수 설계(R9~R11)·트리거(A-2)·오류 처리 구조(R13)는 들어와 있었는데
**봇 메타만 담을 자리가 없었다** — 스키마 최상위에 필드 자체가 없었다.

우리 정답 기준은 "비전문가가 흐름도를 보고 Control Room에 손으로 넣으면 돌아간다"인데,
그 사람이 제일 먼저 만나는 건 액션이 아니라 **저장 대화상자**다.

이 파일이 지키는 것은 하나로 압축된다: **근거 없는 값을 확신 있게 내놓지 않는다.**
`name`만 LLM 제안이고 나머지 셋은 결정론이거나 자리표시자다. 특히 `folder`를 그럴듯하게
지어내면(`Bots/Finance`) 사용자가 그대로 옮겨 존재하지 않는 폴더를 만든다.
"""

import pytest

from app.agent.v4.orchestrator.bot_meta import FOLDER_PLACEHOLDER, fill_bot_meta
from app.schemas.recommendation import BotMeta, Recommendation


def _flow(**kw) -> dict:
    base = {
        "schema_version": "1.0",
        "steps": [{"step_id": "step-1", "label": "s", "actions": [
            {"package": "Excel advanced", "action": "Open", "order": 1, "parameters": []},
        ]}],
    }
    base.update(kw)
    return base


# ── 결정론 필드 — LLM에 묻지 않는다 ──────────────────────────────────────────

def test_트리거가_있으면_무인_실행으로_읽는다():
    """R15가 '트리거 있으면 사실상 무인 실행'으로 이미 쓰는 판단이다 — 같은 결론이어야 한다."""
    flow = fill_bot_meta(_flow(trigger={"kind": "trigger", "title": "매일 9시"}))
    assert flow["bot_meta"]["run_mode"] == "unattended"


def test_트리거가_없으면_대화형_실행으로_읽는다():
    assert fill_bot_meta(_flow())["bot_meta"]["run_mode"] == "attended"


def test_대상_OS를_검수기와_같은_출처에서_읽는다():
    """따로 판단하면 R16 경고와 출력이 어긋난다 — '맥OS 경고'인데 메타는 Windows인 상태."""
    from app.agent.v4.verify.checker import target_os

    flow = _flow(spec={"goal": "g", "assumptions": ["실행 환경: macOS 러너 (사용자 지정)"]})
    fill_bot_meta(flow)
    assert flow["bot_meta"]["target_os"] == "macos" == target_os(flow)


def test_전제가_없으면_검수기_기본값을_따른다():
    """기본값을 여기서 따로 정하면 두 곳이 갈린다."""
    from app.agent.v4.verify.checker import DEFAULT_TARGET_OS

    assert fill_bot_meta(_flow())["bot_meta"]["target_os"] == DEFAULT_TARGET_OS


@pytest.mark.parametrize("llm_value", ["macos", "linux", "windows"])
def test_LLM이_준_결정론_필드는_버린다(llm_value):
    """결정론 판단이라 LLM 값을 믿을 이유가 없고, 믿으면 R15/R16과 어긋날 수 있다."""
    flow = _flow(bot_meta={"name": "n", "target_os": llm_value, "run_mode": "unattended"})
    fill_bot_meta(flow)
    assert flow["bot_meta"]["target_os"] == "windows"   # 전제 없음 → 기본값
    assert flow["bot_meta"]["run_mode"] == "attended"   # 트리거 없음


# ── folder — 지어내지 않는다 ─────────────────────────────────────────────────

def test_폴더는_기본이_자리표시자다():
    """사용자 작업공간 경로는 업무 데이터지 동작 옵션이 아니다(제약 #10)."""
    assert fill_bot_meta(_flow())["bot_meta"]["folder"] == FOLDER_PLACEHOLDER


@pytest.mark.parametrize("invented", ["Bots/Finance", "\\\\share\\bots", "C:/AA/Bots"])
def test_LLM이_지어낸_폴더_경로는_자리표시자로_되돌린다(invented):
    """🔴 그럴듯한 경로가 제일 위험하다 — 사용자가 그대로 옮겨 없는 폴더를 만든다.

    빈칸은 사람이 알아채지만 `Bots/Finance`는 알아채지 못한다.
    """
    flow = _flow(bot_meta={"name": "n", "folder": invented})
    fill_bot_meta(flow)   # 생성 경로 — trust_folder 기본 False
    assert flow["bot_meta"]["folder"] == FOLDER_PLACEHOLDER


def test_편집_경로에서만_사용자_폴더가_보존된다():
    """🔴 문자열만 봐서는 LLM이 지어낸 경로와 사람이 정한 경로를 **구분할 수 없다.**

    그래서 값이 아니라 호출 경로로 가른다. 같은 입력이 경로에 따라 다르게 처리되는 것이
    이 설계의 핵심이라, 두 방향을 한 테스트에서 나란히 못 박는다.
    """
    user_path = "내 작업공간/재무자동화"

    generated = fill_bot_meta(_flow(bot_meta={"folder": user_path}))
    assert generated["bot_meta"]["folder"] == FOLDER_PLACEHOLDER, "생성 경로는 믿을 근거가 0이다"

    edited = fill_bot_meta(_flow(bot_meta={"folder": user_path}), trust_folder=True)
    assert edited["bot_meta"]["folder"] == user_path


# ── name — 유일하게 제안하는 값 ─────────────────────────────────────────────

def test_LLM이_준_이름을_존중한다():
    flow = _flow(bot_meta={"name": "  엑셀 매출 집계 후  메일 발송 "})
    fill_bot_meta(flow)
    assert flow["bot_meta"]["name"] == "엑셀 매출 집계 후 메일 발송"


def test_이름이_비면_목표_문장에서_만든다():
    """빈칸보다 낫고, 사람이 바꾸면 그만인 값이다."""
    flow = _flow(spec={"goal": "매일 환율을 조회해 엑셀에 기록한다"})
    fill_bot_meta(flow)
    assert flow["bot_meta"]["name"] == "매일 환율을 조회해 엑셀에 기록한다"


@pytest.mark.parametrize("junk", ["", "   ", "‹봇 이름›", "null", "미정", 42, None])
def test_못_쓸_이름은_폴백한다(junk):
    """자리표시자 기호를 그대로 돌려주는 경우가 있다 — 이름 자리에 들어가면 파일명이 깨진다."""
    flow = _flow(spec={"goal": "환율 조회"}, bot_meta={"name": junk})
    fill_bot_meta(flow)
    assert flow["bot_meta"]["name"] == "환율 조회"


def test_이름_길이를_제한한다():
    flow = _flow(bot_meta={"name": "가" * 300})
    fill_bot_meta(flow)
    assert len(flow["bot_meta"]["name"]) <= 80


def test_목표도_이름도_없으면_None을_남긴다():
    """지어낼 근거가 하나도 없을 때 조용히 뭔가를 만들지 않는다 ('모름 → 침묵')."""
    flow = fill_bot_meta(_flow())
    assert flow["bot_meta"]["name"] is None


# ── 경계 ─────────────────────────────────────────────────────────────────────

def test_빈_흐름도에는_메타를_붙이지_않는다():
    """액션이 하나도 없는 실패 산출물에 메타만 붙으면 '만들어진 봇'처럼 보인다."""
    assert "bot_meta" not in fill_bot_meta({"schema_version": "1.0", "steps": []})


def test_메타가_dict가_아니어도_죽지_않는다():
    for junk in (None, "meta", 42, []):
        flow = fill_bot_meta(_flow(bot_meta=junk))
        assert isinstance(flow["bot_meta"], dict)


# ── 스키마 하위호환 ─────────────────────────────────────────────────────────

def test_bot_meta_없는_흐름도가_그대로_검증된다():
    """v1~v3가 **같은 스키마**를 검증한다 — 필수 필드로 넣으면 옛 버전이 통째로 깨진다."""
    rec = Recommendation.model_validate({
        "schema_version": "1.0",
        "steps": [{"step_id": "s1", "label": "l", "actions": []}],
    })
    assert rec.bot_meta is None


def test_bot_meta가_붙은_흐름도도_검증된다():
    rec = Recommendation.model_validate(fill_bot_meta(_flow(spec={"goal": "환율 조회"})))
    assert rec.bot_meta.name == "환율 조회"
    assert rec.bot_meta.run_mode == "attended"


def test_알_수_없는_OS는_스키마가_거부한다():
    """Literal이라 'linux' 같은 값이 새면 검증에서 잡힌다 — 결정론 필드의 마지막 방어선."""
    with pytest.raises(Exception):
        BotMeta.model_validate({"target_os": "linux"})


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_생성_경로가_트리거_뒤에_메타를_채운다():
    """🔴 순서가 뒤집히면 트리거 있는 흐름이 attended로 잘못 나간다.

    소스를 읽어 확인한다 — generate_node를 실제로 부르면 LLM에 붙는다.
    """
    import inspect

    from app.agent.v4.orchestrator import generate as gen

    src = inspect.getsource(gen)
    assert src.index('flow["trigger"] = trigger') < src.index("fill_bot_meta(flow)"), \
        "트리거를 붙이기 전에 메타를 채우면 run_mode가 틀린다"


def test_편집_경로도_메타를_갱신한다():
    """'맥OS로 바꿔줘'가 전제를 갈아끼우는데 메타를 다시 안 읽으면 값이 얼어붙는다."""
    import inspect

    from app.agent.v4.orchestrator import edit as edit_mod

    src = inspect.getsource(edit_mod.edit_node)
    assert "fill_bot_meta(result[\"flow\"], trust_folder=True)" in src,         "편집 경로가 trust_folder를 안 켜면 사용자가 정한 폴더가 매 수정마다 지워진다"


def test_compose_프롬프트가_이름만_요구한다():
    """결정론 필드를 LLM에 물으면 R15/R16과 어긋나는 값이 들어온다."""
    from pathlib import Path

    import app.agent.v4 as v4

    md = (Path(v4.__file__).parent / "prompts" / "compose_v4_addendum.md").read_text(encoding="utf-8")
    assert "bot_meta" in md and "name" in md
    assert "folder·target_os·run_mode는 코드가 결정론으로 채운다" in md
