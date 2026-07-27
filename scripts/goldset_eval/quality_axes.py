# -*- coding: utf-8 -*-
"""납득 기준 4축 중 L1(문서 근거성)·L3(독립 LLM 심판) — 설계 제약 #13, §4 측정 계층.

## 왜 이 두 축이 필요한가

계획서 제약 #13은 납득 기준을 **4개 모두**로 확정했다: 문서 근거성 · 문서 유래 골드셋
재현율 · 독립 LLM 심판 · 소수 실측 상관. 그런데 하네스에는 재현율(L2)만 있었다.

두 축이 **재현율이 답하지 못하는 질문**에 답한다:

| 축 | 답하는 질문 | 재현율이 못 답하는 이유 |
|---|---|---|
| L1 근거성 | 이 액션을 **왜** 골랐는가 — 문서가 뒷받침하는가 | 정답과 우연히 맞은 것과 근거 있게 맞은 것을 구별 못 한다 |
| L3 심판 | 이 흐름도가 **실제로 돌아가는가** | 정답 봇을 얼마나 베꼈는지만 잰다 (대안 경로는 0점) |

L3가 특히 중요한 이유는 실측으로 드러났다: 케이스 01에서 에이전트가 `Jira/Create project`를
냈는데 정답 봇이 `Rest/restPost`라 0점이었다. 전용 패키지 쪽이 더 관용적인데도 감점이다.
L3는 정답 봇을 안 보므로 그 편향이 없다.

## 비용이 갈린다 — 그래서 켜는 방식도 갈린다

- **L1은 결정론이다.** LLM·DB를 안 탄다. 흐름도 dict만 읽으므로 **항상 낸다.**
- **L3는 케이스당 LLM 2콜이다**(기대 생성 1 + 반증 1). 13케이스×3반복이면 78콜이라
  런 비용이 늘어난다. **명시적 opt-in**(`--judge`)으로만 켠다.

## L3는 파이프라인 심판과 **같은 함수**를 쓴다

`judge.blind_expectations`/`refute_flow`를 부른다. 여기서 기대 목록을 따로 만들면
파이프라인이 쓰는 기대와 채점이 쓰는 기대가 갈려, "심판 점수가 올랐다"가 무엇을 뜻하는지
알 수 없게 된다.
"""

import logging
import re

logger = logging.getLogger("goldset_eval")

# 결정론 보완으로 붙는 구조 액션은 검색이 아니라 **카탈로그 직조회**로 들어온다
# (`structural_complement`). 문서 인용이 없는 게 정상이라 근거성 분모에서 뺀다 —
# 안 빼면 "구조를 잘 갖출수록 근거성이 떨어진다"는 거꾸로 된 지표가 된다.
_STRUCTURAL_PACKAGES = frozenset({
    "loop", "if", "error handler", "errorhandler", "step", "trigger loop", "comment",
})

# 값의 출처(`ActionParameter.value_source`) 중 **근거 있는** 것.
#   schema_default — 카탈로그 기본값 그대로
#   user           — 사용자가 지정
#   llm            — 모델이 정함 = 근거 없음
_GROUNDED_VALUE_SOURCES = frozenset({"schema_default", "user"})

# 세션을 여는 액션 이름 패턴 — `app/agent/knowledge/derive.py:_OPENER_RE`와 같은 규칙.
# 두 곳이 갈리면 검수기가 잡는 결함과 채점이 세는 결함이 달라진다.
_OPENER_RE = re.compile(r"^\s*(open|connect|start\s+session|log\s?in)\b", re.IGNORECASE)

L1_ROW_KEYS = ("action_cited", "param_grounded", "n_action_cited", "n_action_citable",
               "session_pkg_breaks", "scaffold_claims")
L3_ROW_KEYS = ("judge_met_rate", "judge_soundness", "judge_unmet_must", "judge_fatal")


def _iter_actions(flow: dict):
    """흐름도의 모든 액션을 트리 순회로 낸다 (컨테이너 children 포함)."""

    def walk(actions):
        for a in actions or []:
            if isinstance(a, dict):
                yield a
                yield from walk(a.get("children"))

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            yield from walk(step.get("actions"))


def _is_structural(action: dict) -> bool:
    return (action.get("package") or "").strip().lower() in _STRUCTURAL_PACKAGES


def _norm_pkg(name: str) -> str:
    """패키지 비교 키 — "Excel advanced"와 "Excel advanced 패키지"를 같게 본다."""
    return re.sub(r"(패키지|package)$", "", (name or "").strip(), flags=re.I).replace(" ", "").lower()


# ── L1 근거성 (결정론, 비용 0) ───────────────────────────────────────────────

def groundedness(flow: dict) -> dict:
    """액션·파라미터가 **문서 인용으로 뒷받침되는 비율** (설계 §4 L1).

    `action_cited`  — 검색 히트가 붙은 액션 / 인용 가능한 액션.
        `sources`는 `_attach_sources`가 검색 sink에서 (package, action)으로 맞춰 채운다.
        비어 있다는 건 **그 액션이 검색에 한 번도 안 잡혔다**는 뜻이다 — 모델이 기억에서
        꺼냈거나 결정론 보완으로 들어왔거나 둘 중 하나다. 앞쪽이 환각의 온상이다.
        구조 액션은 분모에서 뺀다(위 상수 주석).

    `param_grounded` — 값이 있는 파라미터 중 출처가 `llm`이 아닌 비율.
        `llm`은 모델이 정한 값이다. 동작 옵션에서 이게 높으면 비전문가가 못 고치는 값을
        모델이 임의로 정하고 있다는 뜻이다(제약 #10).

    ⚠️ 이건 **근거의 존재**를 재지 근거의 적절성을 재지 않는다. 엉뚱한 문서가 붙어도
    1.0이 나온다. 적절성은 L3(심판)의 몫이다 — 두 축을 나란히 봐야 하는 이유다.
    """
    citable = [a for a in _iter_actions(flow) if not _is_structural(a)]
    cited = [a for a in citable if a.get("sources")]

    n_param = n_grounded = 0
    for a in _iter_actions(flow):
        for p in a.get("parameters") or []:
            if not isinstance(p, dict) or p.get("value") in (None, ""):
                continue
            n_param += 1
            if p.get("value_source") in _GROUNDED_VALUE_SOURCES:
                n_grounded += 1

    return {
        "action_cited": round(len(cited) / len(citable), 3) if citable else None,
        "param_grounded": round(n_grounded / n_param, 3) if n_param else None,
        "n_action_cited": len(cited),
        "n_action_citable": len(citable),
        "n_param_valued": n_param,
    }


# ── 흐름 내부 일관성 (결정론, 비용 0) ───────────────────────────────────────
#
# 🔴 **왜 별도 축이 필요한가.** 기존 축은 전부 "정답 대비"다 — 재현율·정밀도·등가 모두
# 정답 봇과 대조한다. 그런데 실사용에서 나온 치명 결함은 정답과 무관하게 **흐름 자체가
# 모순**인 형태였다(2026-07-27 실측):
#
#     3.3  Excel advanced      / Open        produces sExcelSession
#     3.5  Microsoft 365 Excel / Paste cell  consumes sExcelSession   ← 실행 시 여기서 멈춤
#
# 이걸 어느 축도 못 잡는다. 특히 **기능 등가 축은 오히려 성공으로 센다** — 두 패키지가
# 같은 `spreadsheet` 도메인이라 "대안 경로"로 인정해 버린다. 등가 축은 "정답이 A인데
# 에이전트가 B를 썼다"를 봐주려던 장치고, **한 흐름 안에서의 일관성은 보지 않는다.**
# 결함을 성공으로 세는 지표 위에서는 개선을 측정할 수 없어 축을 따로 낸다.
#
# 검수기(R17·R18)와 **같은 결함**을 세지만 목적이 다르다: 검수기는 그 흐름을 고치고,
# 여기서는 런 전체에서 몇 건 남았는지를 센다.

def flow_consistency(flow: dict) -> dict:
    """흐름 내부 모순 — 정답과 무관하게 **실행하면 깨지는** 것만 센다.

    `session_pkg_breaks` — 세션 핸들을 연 패키지와 다른 패키지가 소비한 건수 (R17).
    `scaffold_claims`    — 실행되지 않는 구획(Step/Comment)이 req_id를 달고 있는 건수 (R18).

    검수기를 그대로 부르지 않고 여기서 다시 세는 이유: 채점은 **저장된 산출물**만 보고
    돌아야 한다(재채점이 API 호출 0회라는 계약). 검수기는 카탈로그(DB)를 필요로 한다.
    대신 판정 기준은 같게 유지한다 — 세션 판정만 카탈로그 없이 가능한 근사로 바꾼다.

    ⚠️ 근사의 한계: 카탈로그 opener 목록 없이는 "이 변수가 세션 핸들인가"를 확정할 수
    없어, **액션 이름이 여는 동작**(open/connect/start session/log in)인 것을 opener로 본다
    (`app/agent/knowledge/derive.py`의 `_OPENER_RE`와 같은 패턴). 그래서 검수기보다
    적게 잡을 수 있다 — 이 축은 **하한**이지 정확한 개수가 아니다.
    """
    handle_owner: dict[str, str] = {}
    breaks = 0
    scaffolds = 0

    for a in _iter_actions(flow):
        pkg = (a.get("package") or "").strip()
        act = (a.get("action") or "").strip()
        if not pkg:
            continue

        if _is_structural(a) and not a.get("children") and a.get("req_id"):
            scaffolds += 1

        produces = [r.get("name") for r in a.get("produces") or []
                    if isinstance(r, dict) and r.get("name")]
        opens = bool(_OPENER_RE.match(act)) or any(
            (r.get("role") or "").lower() == "session"
            for r in a.get("produces") or [] if isinstance(r, dict)
        )
        if opens:
            for name in produces:
                handle_owner[name] = pkg

        for r in a.get("consumes") or []:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            owner = handle_owner.get(r["name"])
            if owner and _norm_pkg(owner) != _norm_pkg(pkg):
                breaks += 1

    return {"session_pkg_breaks": breaks, "scaffold_claims": scaffolds}


# ── L3 독립 심판 (LLM 2콜, opt-in) ───────────────────────────────────────────

def judge_axis(flow: dict, spec: dict, document: str | None = None,
               violations: list[dict] | None = None) -> dict | None:
    """정답 봇을 **보지 않고** 흐름도를 채점한다 (LLM 2콜 — 기대 생성 + 반증).

    실패하면 None을 돌려준다. 0점이 아니다 — "측정하지 못했다"와 "나쁜 흐름도"를 같은
    숫자로 만들면 그 축은 못 읽는다(반증 심판 구현에서 실제로 났던 결함과 같은 부류).
    """
    from app.agent.v4.orchestrator.judge import (
        blind_expectations,
        refute_flow,
        score_expectations,
    )

    try:
        expectations = blind_expectations(spec, document)
    except Exception as e:  # noqa: BLE001 — 채점 축 하나가 케이스 전체를 죽이지 않는다
        logger.warning("L3 기대 생성 실패 — 심판 축 생략: %s", e)
        return None
    if not expectations:
        logger.warning("L3 기대가 0건 — 심판 축 생략 (대조 기준이 없다)")
        return None

    try:
        refutation = refute_flow(spec, expectations, flow, violations)
    except Exception as e:  # noqa: BLE001
        logger.warning("L3 반증 실패 — 심판 축 생략: %s", e)
        return None

    scored = score_expectations(expectations, refutation)
    return {
        **scored,
        "expectations": expectations,      # 감사용 — 무엇을 기대했는지 없으면 점수를 못 읽는다
        "defects": refutation.get("defects") or [],
    }


# ── 행 조립 (run_eval·rescore 공용) ──────────────────────────────────────────

def l1_row(flow: dict) -> dict:
    """요약 행에 얹을 L1 + 흐름 내부 일관성 키. 항상 낸다(결정론, LLM 0콜)."""
    merged = {**groundedness(flow), **flow_consistency(flow)}
    return {k: merged[k] for k in L1_ROW_KEYS}


def l3_row(judged: dict | None) -> dict:
    """요약 행에 얹을 L3 키. 축을 못 냈으면 **빈 dict** — 0으로 채우지 않는다."""
    if not judged:
        return {}
    return {
        "judge_met_rate": judged.get("met_rate"),
        "judge_soundness": judged.get("soundness"),
        "judge_unmet_must": judged.get("n_unmet_must"),
        "judge_fatal": judged.get("n_fatal"),
    }
