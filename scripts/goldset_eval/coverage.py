"""요구사항 커버리지 채점 (평가 지표 A) — 정답 봇과 독립.

문자열 매칭 F1은 정답 봇의 미명시 구현 접착제(경로 조립 assign·폴더 생성·로깅·날짜
포맷)까지 재현을 요구해 recall을 구조적으로 눌렀다(정답/문서 비율 3~5배, 놓친 정답의
53%가 문서 무명시 접착제). 이 지표는 다른 질문을 던진다:

    "업무정의서가 **명시한** 작업들을, 에이전트 흐름도가 실제로 달성했는가?"

정답 봇이 아니라 **문서**를 기준으로 채점하므로, 문서가 성긴 만큼 요구도 성기고, 에이전트가
합리적으로 추론해 채운 부분은 감점하지 않는다. 정답 봇의 구현 세부는 채점에 개입하지 않는다.

절차(LLM 심판 1회, 정답 봇 미투입):
  1) 업무정의서에서 검증 가능한 '작업 요구' 목록을 추출한다(작업 순서 항목 단위).
  2) 각 요구를 에이전트 흐름도 아웃라인이 covered/partial/missing 중 무엇으로 달성하는지 판정.
  3) coverage = (covered + 0.5·partial) / total.

purpose="recommend"로 관측성 래퍼를 타 사용량이 귀속된다. 실패 시 None(지표 결측)으로
강등하고 F1 파이프라인은 계속 간다(부분 실패 격리).
"""

import json
import logging

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 RPA 자동화 검수관이다. [업무정의서]가 요구한 작업들을 [흐름도]가
실제로 달성하는지 항목별로 판정한다.

원칙:
- 채점 기준은 오직 [업무정의서]다. 문서에 명시된 '작업 순서' 항목을 검증 가능한 요구로 삼는다.
  문서가 말하지 않은 구현 세부(경로 조립, 폴더 존재 확인, 로깅, 날짜 포맷, 예외 처리 등)는
  요구로 넣지 않는다 — 그건 자동화 도구가 알아서 채우는 부분이다.
- 각 요구가 흐름도에서 달성되는지 판정한다:
  - "covered": 그 작업을 수행하는 액션이 흐름도에 있다(액션 이름이 달라도 기능이 맞으면 인정 —
    예: 문서 '메일 발송'을 Email 전송이든 Outlook 전송이든 수행하면 covered).
  - "partial": 관련 액션은 있으나 불완전하다(예: 파일 열었는데 저장/닫기 없음, 조건 일부만).
  - "missing": 그 작업을 수행하는 액션이 없다.
- 흐름도가 문서에 없는 작업을 더 했어도 감점하지 않는다(그건 F1이 볼 몫). 여기선 오직 '문서
  요구의 달성률'만 본다.

출력은 아래 JSON 하나만:
{
  "requirements": [
    {"text": "문서가 요구한 작업 한 줄", "status": "covered|partial|missing",
     "evidence": "흐름도에서 이를 달성한 액션(없으면 왜 없는지)"}
  ]
}"""


def _render_flow_outline(flow: dict) -> str:
    """흐름도를 사람이 읽는 아웃라인으로. edit_ops.render_outline 재사용(있으면)."""
    try:
        import copy

        from app.agent.v3.orchestrator.edit_ops import annotate_ids, render_outline

        return render_outline(annotate_ids(copy.deepcopy(flow)))
    except Exception:  # noqa: BLE001 — 폴백: 직접 렌더
        lines: list[str] = []

        def walk(actions, depth):
            for a in actions or []:
                lines.append("  " * depth + f"- {a.get('package')}/{a.get('action')} [{a.get('label') or ''}]")
                walk(a.get("children"), depth + 1)

        for s in flow.get("steps", []):
            lines.append(f"[{s.get('step_id')}] {s.get('label') or ''}")
            walk(s.get("actions"), 1)
        return "\n".join(lines)


def score_coverage(doc_text: str, flow: dict, *, purpose: str = "recommend") -> dict | None:
    """업무정의서 요구 대비 흐름도 커버리지. 반환 {requirements, coverage, n_*} 또는 None."""
    if not doc_text or not (flow.get("steps") or []):
        return {"requirements": [], "coverage": None, "n_covered": 0, "n_partial": 0,
                "n_missing": 0, "n_total": 0}

    from app.core import llm

    outline = _render_flow_outline(flow)
    user = f"[업무정의서]\n{doc_text}\n\n[흐름도]\n{outline}"
    try:
        raw = llm.chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            purpose=purpose,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — 심판 실패는 지표 결측일 뿐
        logger.warning("커버리지 심판 실패: %s", e)
        return None

    reqs = data.get("requirements") or []
    n_cov = sum(1 for r in reqs if r.get("status") == "covered")
    n_par = sum(1 for r in reqs if r.get("status") == "partial")
    n_mis = sum(1 for r in reqs if r.get("status") == "missing")
    total = n_cov + n_par + n_mis
    coverage = round((n_cov + 0.5 * n_par) / total, 3) if total else None
    return {
        "requirements": reqs,
        "coverage": coverage,
        "n_covered": n_cov,
        "n_partial": n_par,
        "n_missing": n_mis,
        "n_total": total,
    }
