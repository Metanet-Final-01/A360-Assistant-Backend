"""check — 생성된 단계 시퀀스를 검수하고 sources·confidence를 부착한다.

R1~R6 정적 체커를 돌려 위반을 모으고(repair 트리거·notes 후보용), 각 액션에는
**RAG 검색 근거(grounding) 기반 신뢰도**를 액션별로 매긴다 (FR-12). "이 액션이
업무 단계에 적합한가"를 KB가 그 액션을 얼마나 강하게 연관지었는지(검색 score)로
나타낸다 — LLM 자기평가가 아니라 결정론적 산출이라 평가지표로 방어 가능하다.
sources는 shortlist가 만든 source_map에서 액션별로 부착한다(FR-11 근거).

신뢰도 산출(1차):
  - 검색으로 뒷받침된 액션        → 검색 score를 신뢰도로 사상 (강한 매칭일수록 높음)
  - 결정론 주입 컨테이너(Loop/If)  → 구조적으로 확실 (검색 대상이 아님)
  - 근거 없는(검색 미히트) 액션    → 낮음 (LLM이 KB 뒷받침 없이 고른 것)

⚠️ 정확한 산출 근거(검색 score와 검수 위반 결과를 어떻게 결합·가중·보정할지)는
   팀 합의 후 확정 예정이다. 아래 상수와 _grounding_confidence()가 그 조정 지점이다.
   현재는 검색 근거만 반영하는 1차 산식이다(검수 위반은 confidence에 아직 미반영 —
   위반 액션이라도 검색 score가 높으면 높은 신뢰도가 나올 수 있음. 팀 논의 대상).
"""

from ..verify.catalog import CatalogLookup
from ..verify.checker import CONTAINER_ACTIONS, run_checks

# ── 액션별 신뢰도(FR-12) — RAG 검색 근거(grounding) 기반 1차 산식 ──────────────
# 검색 score는 재정렬(rerank) 상대점수로 대략 0~1 범위다. 값이 높을수록 KB가 이
# 액션을 해당 작업 단계와 강하게 연관지었다는 뜻이라 신뢰도로 그대로 사상한다.
# 아래 두 상수는 검색 대상이 아닌 액션의 신뢰도 — 팀 합의 후 조정 대상이다.
_CONF_STRUCTURAL = 0.85  # Loop/If 등 결정론 주입 컨테이너 — 검색 대상 아님, 구조적으로 확실
_CONF_UNGROUNDED = 0.3   # 검색 히트 없이 LLM이 자체 선택한 액션 — KB 근거 약함


def _grounding_confidence(score: float | None) -> float:
    """검색 score → 신뢰도 [0,1]. 1차: 선형 사상(팀 합의 후 정교화 예정).

    score가 None(근거는 있으나 점수 미상)이면 근거 약함으로 본다.
    """
    if score is None:
        return _CONF_UNGROUNDED
    return round(min(1.0, max(0.0, float(score))), 2)


def _action_confidence(action: dict, source_map: dict) -> float:
    """액션 하나의 신뢰도를 검색 근거로 산정한다 (RAG grounding).

    우선순위: 검색으로 뒷받침(source_map 히트) > 결정론 컨테이너 > 근거 없음.
    """
    key = f"{action.get('package')}/{action.get('action')}"
    source = source_map.get(key)
    if source is not None:
        return _grounding_confidence(source.get("score"))
    if (action.get("package"), action.get("action")) in CONTAINER_ACTIONS:
        return _CONF_STRUCTURAL
    return _CONF_UNGROUNDED


def _attach(action: dict, source_map: dict) -> list[float]:
    """액션 트리에 sources(없으면)·confidence(액션별 검색 근거 기반)를 재귀 부착한다.

    반환: 이 서브트리(자기+자손) 액션들의 confidence 목록 (단계 요약 집계용).
    이미 confidence가 있으면(예: 상위에서 지정) 보존한다.
    """
    key = f"{action.get('package')}/{action.get('action')}"
    if not action.get("sources") and source_map.get(key):
        src = source_map[key]
        action["sources"] = [{
            "source_type": "action_schema",
            "title": src.get("title"),
            "url": src.get("url"),
            "score": src.get("score"),
        }]
    if action.get("confidence") is None:
        action["confidence"] = _action_confidence(action, source_map)

    collected = [action["confidence"]]
    for child in action.get("children") or []:
        collected.extend(_attach(child, source_map))
    return collected


def check(
    actions: list[dict],
    catalog: CatalogLookup,
    source_map: dict | None = None,
) -> dict:
    """단계 시퀀스를 검수하고 액션별 신뢰도·근거를 부착한다.

    반환: {violations: [dict], confidence: float, actions: [dict]}.
    - violations: R1~R6 정적 검수 위반 (harness의 repair 트리거·notes 후보).
    - confidence: 단계 요약 신뢰도 = 액션별 신뢰도의 최솟값(가장 약한 액션 기준,
      보수적 요약). 실질 소비는 각 액션의 confidence 필드이고 이 스칼라는 로깅·진단용.
    - actions: sources·confidence가 부착된 계열(입력 dict를 제자리 보강).
    """
    violations = run_checks(actions, catalog)

    source_map = source_map or {}
    scores: list[float] = []
    for action in actions:
        scores.extend(_attach(action, source_map))

    return {
        "violations": [v.as_dict() for v in violations],
        "confidence": round(min(scores), 2) if scores else 0.0,
        "actions": actions,
    }
