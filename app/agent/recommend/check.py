"""check — 생성된 단계 시퀀스를 검수하고 confidence·sources를 부착한다.

R1~R6 정적 체커를 돌려 위반을 모으고, 위반 수와 (compose의) 스키마 교정 여부로
confidence를 결정론적 밴드로 매긴다 — LLM 자기평가가 아니라 검수 결과 기반이라
평가지표로 방어 가능하다. sources는 shortlist가 만든 source_map에서 액션별로 부착한다
(FR-11 근거). 검수 위반 기반 국소 repair 루프는 후속(RPA-27b).
"""

from ..verify.catalog import CatalogLookup
from ..verify.checker import run_checks

# confidence 밴드 (FR-12). LLM 주장 대신 검수 결과로 산정.
_CONF_CLEAN = 0.9  # 무위반
_CONF_SCHEMA_REPAIRED = 0.7  # compose가 스키마 교정을 거침
_CONF_HAS_VIOLATIONS = 0.5  # 미해결 위반 잔존


def _attach(action: dict, source_map: dict, confidence: float) -> None:
    """액션 트리에 sources(없으면)·confidence(없으면)를 재귀 부착한다."""
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
        action["confidence"] = confidence
    for child in action.get("children") or []:
        _attach(child, source_map, confidence)


def check(
    actions: list[dict],
    catalog: CatalogLookup,
    source_map: dict | None = None,
    *,
    schema_repaired: bool = False,
) -> dict:
    """단계 시퀀스를 검수한다.

    반환: {violations: [dict], confidence: float, actions: [dict]}.
    actions는 sources·confidence가 부착된 사본 계열(입력 dict를 제자리 보강).
    """
    violations = run_checks(actions, catalog)
    if violations:
        confidence = _CONF_HAS_VIOLATIONS
    elif schema_repaired:
        confidence = _CONF_SCHEMA_REPAIRED
    else:
        confidence = _CONF_CLEAN

    source_map = source_map or {}
    for action in actions:
        _attach(action, source_map, confidence)

    return {
        "violations": [v.as_dict() for v in violations],
        "confidence": confidence,
        "actions": actions,
    }
