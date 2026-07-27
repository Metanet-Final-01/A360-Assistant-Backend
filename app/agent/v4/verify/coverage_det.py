"""결정론 요구 커버리지 — "담당 액션이 하나도 없는 요구"(누락)와 "한 자리가 요구 여럿을
떠맡은 것"(뭉갬)을 LLM 없이 판정한다 (설계 §5.2-A·B, Phase 3).

**왜 필요한가.** 검수 스택 전체가 '잘못 넣은 것'만 본다(R1 환각은 blocker인데, 있어야 할
액션을 안 넣으면 어떤 규칙도 발화하지 않는다 — 설계 §5.1-③). 그래서 모델의 합리적 전략이
"확신 없으면 빼기"가 되고, 교정 루프의 목적 함수(정적 위반 가중합)까지 삭제를 보상한다.
실측 서명: 정밀도 0.295→0.430(상승) vs 재현율 0.109→0.151(정체). 누락을 환각과 **동급
blocker**로 올려 넣기/빼기의 무게를 대칭화하는 것이 이 모듈의 1차 목적이다.

**뭉갬은 누락의 대칭짝이다.** 사용자 불만은 "빠뜨리거나 **뭉갠다**" 둘이었는데, 뭉갬은
지금껏 아무도 안 봤다. 게다가 뭉갬은 그냥 안 잡히는 정도가 아니라 **누락으로 둔갑해**
교정을 엉뚱한 방향으로 끌고 갔다(아래 `slot_req_ids` 참조) — 그래서 누락 판정과 같은
자리에서 함께 본다.

**빈껍데기는 구획 뒤에 숨은 누락이다.** req_id는 붙어 있는데 그 자리가 실행되지 않는
구획(`Step`)이면 배정은 있고 실행은 없다 — 실측에서 "요구 누락 0건"과 R18이 정면으로
어긋났다(아래 `hollow_requirements` 참조). 이건 셋째 도피로였다: 액션을 지우면 누락
blocker가 발화하지만, **이름만 남긴 구획으로 바꾸면 아무 신호도 안 났다.**

**왜 LLM이 아니라 결정론인가.** §5.2-B의 2단 판정 중 1단이다 — "슬롯 미배정"은 문자열
대조로 끝나는 확정 누락이라 오탐이 0이다. "배정됐지만 부적합"은 판단이 필요해 L2
시맨틱(semantic.py, LLM)이 major로 본다. 오탐 0인 신호만 blocker로 쓴다.

**침묵 원칙.** 흐름도 액션에 req_id가 **하나도** 없으면 빈 목록을 낸다(누락도 뭉갬도).
앵커 미기재는 '요구를 안 지켰다'가 아니라 '연결 정보가 없다'이므로, 검사하면 전량 오탐이
된다. checker의 R9/R10이 produces 명시가 하나도 없으면 침묵하는 것과 같은 원칙이다.
"""

import re

from .checker import performs_work
from .findings import Finding

# 한 칸에 요구를 여럿 적을 때 모델이 쓰는 구분자 — 콤마·세미콜론·슬래시·파이프·'+'·공백.
# req_id 자체는 spec.py가 결정론으로 "req-N"을 매기므로 이 문자들을 포함할 수 없다.
_MULTI_SEP = re.compile(r"[,;/|+]|\s+")


def slot_req_ids(action: dict, known: set[str] | None = None) -> list[str]:
    """액션 한 자리가 담당한다고 **주장하는** 요구 id 목록 (등장 순서·중복 제거).

    스키마상 `RecommendedAction.req_id`는 스칼라 하나(`str | None`)라 "한 액션 = 요구
    하나"처럼 보인다. 하지만 검수·교정 루프가 실제로 보는 흐름도는 **Recommendation
    검증을 아직 안 거친 원시 dict**다 — recommend/graph.py가 `_parse_flow`로 LLM JSON을
    떠서 refine_flow에 넘기고, `Recommendation.model_validate`는 finalize 맨 끝에서야
    돈다. 그래서 이 함수에 도착하는 값은 스칼라만이 아니다:

    - `"req-1"`             정상 (한 자리 = 요구 하나)
    - `"req-1, req-2"`      한 자리가 요구 둘을 담당한다는 주장 — 스칼라 칸에 우겨넣은 형태
    - `["req-1","req-2"]`   같은 주장의 리스트 슬립. 바로 앞 단계(operation_units.md)가
                            "여러 요구에 걸치면 `req_ids`에 여러 개"라고 가르치므로 모델이
                            그 모양을 그대로 물고 온다.

    **뒤 둘이 이 파이프라인에서 뭉갬이 나타나는 실제 모양이다.** 그리고 지금까지는 조용히
    해로웠다: 기존 `_assigned_req_ids`는 str이 아닌 값을 버리고 콤마 문자열은 어느 요구와도
    안 맞는 통짜 키로 셌다 → 그 요구들이 전부 '미배정'으로 잡혀 **뭉갬 1건이 누락 blocker
    2건(200)으로 둔갑**한다. surgeon은 "삽입하라"는 지시를 받고 이미 (부실하게) 하고 있는
    일을 하는 액션을 하나 더 넣는다 — 재분해가 아니라 중복 생성이다.

    분해는 **known(스펙 실재 id)이 확인해 줄 때만** 한다. 앵커를 쪼개는 것은 되돌릴 수 없는
    해석이라, 쪼갠 조각 중 실재 요구가 하나도 없으면 원문을 통짜로 남긴다(모름 → 침묵).
    """
    raw = action.get("req_id")
    if isinstance(raw, (list, tuple, set)):
        tokens = [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if known and text not in known:
            parts = [p for p in _MULTI_SEP.split(text) if p]
            tokens = parts if any(p in known for p in parts) else [text]
        else:
            tokens = [text]
    else:
        return []
    out: list[str] = []
    for t in tokens:
        if t not in out:
            out.append(t)
    return out


def _assigned_req_ids(flow: dict, known: set[str] | None = None) -> set[str]:
    """흐름도의 모든 액션(children 재귀 포함)이 들고 있는 req_id 집합."""
    found: set[str] = set()

    def walk(actions) -> None:
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            found.update(slot_req_ids(a, known))
            walk(a.get("children"))

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            walk(step.get("actions"))
    return found


def _spec_requirements(spec: dict) -> list[dict]:
    return [r for r in (spec or {}).get("requirements") or [] if isinstance(r, dict) and r.get("req_id")]


def missing_requirements(flow: dict, spec: dict) -> list[str]:
    """요구 중 담당 액션이 하나도 없는 것 — LLM 없이 판정한다(오탐 0).

    반환 순서는 spec의 요구 순서를 유지한다(사람이 읽는 순서 = 업무 순서).
    흐름도에 req_id 배정이 전무하면 빈 목록 — 모듈 docstring의 침묵 원칙.

    뭉갬 슬롯("req-1, req-2")이 주장하는 요구는 **배정으로 센다.** 담당 액션이 (부실하게나마)
    있는데도 미배정으로 세면 누락 blocker가 발화해 surgeon이 중복 액션을 삽입한다 —
    뭉갬은 뭉갬으로(재분해 지시로) 잡아야 한다.
    """
    reqs = _spec_requirements(spec)
    if not reqs:
        return []
    known = {r["req_id"] for r in reqs}
    assigned = _assigned_req_ids(flow, known)
    if not assigned:
        return []
    return [r["req_id"] for r in reqs if r["req_id"] not in assigned]


def coverage_findings(flow: dict, spec: dict) -> list[Finding]:
    """미배정 must 요구 → blocker Finding. severity는 findings.Finding 규약을 따른다.

    must 미배정 = blocker(=R1 환각과 동급, 가중치 100), should 미배정 = minor.
    from_coverage(L2 LLM 채점)의 등급 규약과 같은 축을 쓴다 — 두 신호가 같은 회귀 가드
    가중합에 섞여도 등급이 어긋나지 않게.

    fix_hint는 surgeon에게 **삭제가 아니라 삽입**을 지시한다. 위반 있는 액션을 지우는
    가장 싼 경로를 막는 것이 결정 A의 요지이므로, 힌트에서도 그 경로를 닫아 둔다.
    """
    by_id = {r["req_id"]: r for r in _spec_requirements(spec)}
    out: list[Finding] = []
    for rid in missing_requirements(flow, spec):
        req = by_id[rid]
        text = str(req.get("text") or "").strip()
        priority = req.get("priority") or "must"
        out.append(
            Finding(
                layer="L2",
                severity="minor" if priority == "should" else "blocker",
                req_id=rid,
                message=f"[{rid}] 미배정: 요구 '{text}'를 담당하는 액션이 흐름도에 없습니다.",
                fix_hint=(
                    f"이 요구를 실현하는 액션을 삽입(insert/wrap)하고 그 액션에 req_id='{rid}'를 "
                    f"부여하라. 기존 액션을 지워서 해결하려 하지 말 것."
                ),
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 뭉갬 — 한 자리가 요구 여럿을 떠맡은 경우 (Phase 3)
# ─────────────────────────────────────────────────────────────────────────────

def conflated_slots(flow: dict, spec: dict) -> list[dict]:
    """요구 2개 이상을 한꺼번에 담당한다고 주장하는 **리프** 액션들.

    반환: [{"step_id", "location", "label", "req_ids"}] — location은 checker/Violation과
    같은 트리 경로 표기("actions[1].children[0]")라 surgeon 프롬프트에서 좌표가 일관된다.

    **컨테이너는 세지 않는다(오탐 방지).** children이 있는 노드(Loop·If·Error handler·Step)는
    본문이 여러 요구를 수행하는 게 정상이고, compose 프롬프트가 실제로 그렇게 지시한다
    (compose_v4_addendum: "컨테이너에도 붙인다 — 본문이 그 요구를 수행한다면"). 컨테이너까지
    세면 잘 만든 흐름도가 통째로 발화한다. 반대로 **리프**가 요구 둘을 들었다는 건 "조작
    하나가 업무 둘을 한다"는 주장이라 재분해 대상이 확정이다.
    컨테이너가 정말로 뭉갠 경우는 놓치지만, 이 모듈의 계약은 '오탐 0인 신호만'이다.

    req_id가 빈 순수 배관(경로 조립·폴더 확인)은 주장하는 요구가 0개라 자연히 침묵한다.
    """
    reqs = _spec_requirements(spec)
    if not reqs:
        return []
    known = {r["req_id"] for r in reqs}
    out: list[dict] = []

    def walk(actions, step_id, base: str) -> None:
        for i, a in enumerate(actions or []):
            if not isinstance(a, dict):
                continue
            location = f"{base}[{i}]"
            children = a.get("children") or []
            if not children:
                claimed = [r for r in slot_req_ids(a, known) if r in known]
                if len(claimed) >= 2:
                    out.append({
                        "step_id": step_id,
                        "location": location,
                        "label": a.get("label") or a.get("action") or "",
                        "req_ids": claimed,
                    })
            walk(children, step_id, f"{location}.children")

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            walk(step.get("actions"), step.get("step_id"), "actions")
    return out


def conflation_findings(flow: dict, spec: dict) -> list[Finding]:
    """뭉갬 → **담긴 요구 하나당 Finding 하나**. must=major, should=minor.

    **왜 blocker가 아닌가.** 누락(blocker·100)은 업무가 흐름도에서 아예 사라진 상태고,
    뭉갬은 업무가 있긴 한데 한 자리에 눌려 있는 상태다 — 등급이 같으면 "쪼개느니 req_id
    하나를 떼자"가 성립할 여지가 생긴다(떼면 뭉갬 blocker가 사라지고 누락 blocker가 생겨
    상쇄). major로 두면 누락(100)이 항상 무거워 **떼기는 절대 이득이 아니다.** 등급은
    "재분해하라"는 지시의 무게지 환각의 무게가 아니라는 뜻이기도 하다.

    **왜 슬롯당 1건이 아니라 요구당 1건인가 — 회귀 가드 산술.** 뭉갬을 푸는 유일한 수리는
    액션을 쪼개 넣는 것(insert)이고, 삽입은 그 자체가 새 정적 위반(파라미터 미충족 등)을
    흔히 만든다(harness.MAX_REFINE_ROUNDS 주석의 실측). 슬롯당 major 1건(10)이면
    "뭉갬 해소(−10) + 부수 위반(+10) = ±0"이 되어 회귀 가드(`new_weight < current_weight`)가
    그 패치를 폐기한다 — **뭉갬을 영원히 못 고친다.** 요구당 1건이면 2중 뭉갬이 20이라
    삽입 1건이 만드는 부수 위반을 흡수하고, 무게가 '풀어야 할 요구 수'에 비례해 수리
    비용과도 맞는다. (should만 둘 뭉친 경우는 3+3=6이라 부수 위반을 못 이긴다 — 의도한
    선이다. 낮은 우선순위 재분해를 위해 새 결함을 감수하진 않는다.)
    """
    by_id = {r["req_id"]: r for r in _spec_requirements(spec)}
    out: list[Finding] = []
    for slot in conflated_slots(flow, spec):
        ids = slot["req_ids"]
        listed = "·".join(ids)
        for rid in ids:
            priority = (by_id.get(rid) or {}).get("priority") or "must"
            text = str((by_id.get(rid) or {}).get("text") or "").strip()
            others = [o for o in ids if o != rid]
            out.append(
                Finding(
                    layer="L2",
                    severity="minor" if priority == "should" else "major",
                    req_id=rid,
                    step_id=slot["step_id"],
                    location=slot["location"],
                    message=(
                        f"[{rid}] 뭉갬: 액션 «{slot['label']}» 한 자리가 요구 {listed}를 "
                        f"한꺼번에 담당합니다 — 요구 '{text}'가 다른 요구와 같은 조작에 눌려 있습니다."
                    ),
                    fix_hint=(
                        f"이 자리를 요구마다 하나씩 나눠라: '{text}'를 수행하는 액션을 따로 두고 "
                        f"req_id='{rid}'를 부여하고, {', '.join(others)}도 각자의 액션으로 분리하라. "
                        f"req_id만 떼어내 담당을 줄이는 것은 해결이 아니다 — 뗀 요구가 즉시 "
                        f"미배정 blocker가 된다."
                    ),
                )
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 빈껍데기 — 담당한다고 주장하는 자리가 아무것도 실행하지 않는 경우 (RPA-298)
# ─────────────────────────────────────────────────────────────────────────────
#
# 🔴 **왜 필요한가 — 실측(2026-07-27).** 같은 산출물에서 두 지표가 정면으로 어긋났다:
#
#     verifying | 검수 위반 20건 · 요구 누락 0건 · 요구 뭉갬 0건
#     R18 major | '3일치 시세를 순서대로 기록'이 요구 req-4를 담당한다고 돼 있는데
#                 Step은 실행되지 않는 구획입니다
#
# req-4는 **must**("최근 3일치 일별 시세를 엑셀에 입력한다")인데 담당이 `Step` 하나뿐이라
# 실행되는 것이 없다. 그런데 `_assigned_req_ids`는 req_id가 붙어 있다는 사실만 보므로
# "누락 0건"이 된다 — 이 모듈이 막으려던 '조용한 삭제'가 **구획 뒤에 숨은** 형태다.
# 액션을 지우는 것보다 나쁘다: 지우면 누락 blocker가 발화하는데, 이름만 남긴 구획으로
# 바꾸면 아무 신호도 안 난다. 즉 **검수를 통과하는 삭제 경로가 하나 열려 있었다.**
#
# 누락과 별개 함수로 두는 이유는 **고칠 방법이 다르기 때문**이다. 누락의 처방은 "삽입"인데,
# 빈껍데기에 삽입을 지시하면 이미 요구를 주장하는 구획 옆에 액션이 하나 더 생겨 중복이 된다
# (`slot_req_ids` 독스트링이 기록한 그 실패 모양). 처방은 "그 자리를 채우거나 대체하라"다.

def hollow_requirements(flow: dict, spec: dict) -> list[dict]:
    """담당 자리가 **전부 비실행 구획**인 요구 — 배정은 됐는데 실행되는 것이 없다.

    반환: [{"req_id", "step_id", "location", "label"}] — location은 checker/Violation과
    같은 트리 경로 표기라 surgeon 프롬프트에서 좌표가 일관된다. 한 요구를 여러 구획이
    나눠 주장하면 **첫 자리**를 좌표로 준다(수리는 어차피 요구 단위로 한다).

    실행되는 자리가 하나라도 있으면 침묵한다 — 구획과 실제 액션이 같은 요구를 함께 들고
    있는 것은 정상이다(구획이 묶고 액션이 한다).

    누락(`missing_requirements`)과는 **상호 배타**다: 여기 잡히는 요구는 배정이 있어
    누락으로는 잡히지 않는다. 그래서 같은 요구가 두 항으로 이중 계상되지 않는다.
    """
    reqs = _spec_requirements(spec)
    if not reqs:
        return []
    known = {r["req_id"] for r in reqs}
    executed: set[str] = set()
    hollow: dict[str, dict] = {}

    def walk(actions, step_id, base: str) -> None:
        for i, a in enumerate(actions or []):
            if not isinstance(a, dict):
                continue
            location = f"{base}[{i}]"
            claimed = [r for r in slot_req_ids(a, known) if r in known]
            if claimed:
                if performs_work(a):
                    executed.update(claimed)
                else:
                    for rid in claimed:
                        hollow.setdefault(rid, {
                            "req_id": rid,
                            "step_id": step_id,
                            "location": location,
                            "label": a.get("label") or a.get("action") or "",
                        })
            walk(a.get("children"), step_id, f"{location}.children")

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            walk(step.get("actions"), step.get("step_id"), "actions")

    # spec 순서를 유지한다 — 사람이 읽는 순서 = 업무 순서.
    return [hollow[r["req_id"]] for r in reqs
            if r["req_id"] in hollow and r["req_id"] not in executed]


def hollow_findings(flow: dict, spec: dict) -> list[Finding]:
    """빈껍데기 → must=blocker, should=minor. 누락(`coverage_findings`)과 같은 등급 축.

    **왜 누락과 같은 blocker인가.** 업무가 실행되지 않는다는 점에서 결과가 동일하다.
    등급을 낮추면 "액션을 지우고 같은 req_id를 든 Step으로 바꾸기"가 가중합을 **낮추는**
    수가 되어, 이 모듈이 막으려던 삭제 편향이 구획을 경유해 되살아난다.

    등급을 누락과 **같게** 두는 것이 게임 이론상으로도 맞다. 구획을 통째로 지우면
    빈껍데기(100)가 사라지고 누락(100)이 생겨 가중합이 그대로다 — 회귀 가드가
    `new_weight < current_weight`를 요구하므로 그 패치는 채택되지 않는다. 값을 낮추면
    지우기가 이득이 되고, 높이면 반대로 지우기가 이득이 된다(둘 다 도피로다).

    R18(major)과 함께 발화하는데 이중 계상이 아니다 — R18은 **자리**를 짚어 surgeon에게
    좌표를 주고, 이쪽은 **요구**가 실현되지 않았음을 말한다. 층이 다르고 처방이 다르다.
    """
    by_id = {r["req_id"]: r for r in _spec_requirements(spec)}
    out: list[Finding] = []
    for slot in hollow_requirements(flow, spec):
        rid = slot["req_id"]
        req = by_id[rid]
        text = str(req.get("text") or "").strip()
        priority = req.get("priority") or "must"
        label = slot["label"] or "이 자리"
        out.append(
            Finding(
                layer="L2",
                severity="minor" if priority == "should" else "blocker",
                req_id=rid,
                step_id=slot["step_id"],
                location=slot["location"],
                message=(
                    f"[{rid}] 빈껍데기: 요구 '{text}'를 «{label}»이(가) 담당한다고 돼 있지만 "
                    f"그 자리는 실행되지 않는 구획이라 아무 일도 일어나지 않습니다."
                ),
                fix_hint=(
                    f"«{label}» 자리를 실제로 실행되는 카탈로그 액션으로 **채우거나 대체**하라 "
                    f"(구획이라면 그 안에 액션을 넣고, 아니라면 액션으로 바꿔라). "
                    f"옆에 액션을 새로 추가하지 말 것 — 구획이 같은 req_id를 계속 들고 있어 "
                    f"중복이 된다. 구획을 지우기만 하는 것도 해결이 아니다 — '{text}'가 즉시 "
                    f"미배정 blocker가 된다."
                ),
            )
        )
    return out


def completeness_findings(flow: dict, spec: dict) -> list[Finding]:
    """완성도 항 전체 — 누락(blocker) + 뭉갬(major) + 빈껍데기(blocker).
    refine 회귀 가드의 비교축에 들어간다.

    셋을 한 함수로 묶는 이유는 소비자(harness)가 항상 함께 쓰기 때문이다: 누락만 재면
    "액션을 지워 위반을 없애기"가, 뭉갬만 재면 "요구를 떼어 뭉갬을 없애기"가, 빈껍데기를
    빼면 "액션을 요구만 든 빈 구획으로 바꾸기"가 각각 열린다. 세 항이 같은 축에 있어야
    도피로가 동시에 막힌다.
    """
    return (
        coverage_findings(flow, spec)
        + conflation_findings(flow, spec)
        + hollow_findings(flow, spec)
    )
