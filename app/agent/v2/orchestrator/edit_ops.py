"""edit 연산(patch) 엔진 — 흐름도 국소 수정을 '전체 재출력' 대신 '작은 연산의 결정론 적용'으로.

기존 edit는 LLM에게 수정된 흐름도 전체를 다시 출력하게 했다. 흐름도가 크면 LLM이
(1) 원본을 그대로 되뱉거나(게으른 에코 — change_summary만 그럴듯), (2) 스크립트 파라미터의
따옴표·개행을 잘못 이스케이프해 JSON이 깨지는 실패가 잦았다. 근본 원인은 "큰 구조를 한 글자도
안 틀리고 다시 써라"라는 요구 자체다.

여기서는 LLM이 노드 id를 참조하는 **작은 수정 연산만** 출력하고(EditOps), 파이썬이 현재
흐름도에 결정론적으로 적용한다. 손대지 않은 노드는 원본 dict 그대로라 파라미터·value_source·
근거가 자동 보존되고, 에코할 원본이 없으니 게으른 에코가 원천 차단된다.

연산 종류:
- wrap        : 연속한 형제 액션들을 새 컨테이너의 children으로 감싼다(+뒤에 형제 컨테이너 추가).
                Try/Catch/Finally·If/Else·Loop 감싸기가 모두 이 하나로 표현된다.
- insert      : anchor 기준 앞/뒤 또는 컨테이너 안(처음/끝)에 새 액션을 넣는다.
- remove      : 노드를 지운다.
- move        : 노드를 anchor 기준 위치로 옮긴다.
- set_params  : 노드 파라미터를 name 기준 병합/치환한다.
- update      : 노드의 package/action/label을 바꾼다.
- set_flow    : 흐름도 수준 notes/variables를 바꾼다.

id는 프롬프트에 보여줄 때만 임시로 붙였다가(_annotate_ids) 적용 후 벗긴다(strip_ids) —
스키마(RecommendedAction)에는 저장하지 않는 관측용 필드다.
"""

from typing import Literal

from pydantic import BaseModel, Field

# 임시 노드 id를 다는 전이(transient) 키 — 프롬프트 참조용, 적용 후 제거한다.
_ID = "_id"

_POSITIONS = frozenset({"before", "after", "into_start", "into_end"})


class EditOp(BaseModel):
    """단일 수정 연산. op에 따라 쓰는 필드가 다르다(플랫 스키마 — LLM 출력 친화)."""

    op: Literal["wrap", "insert", "remove", "move", "set_params", "update", "set_flow"]
    target: str | None = None                 # remove/move/set_params/update: 대상 노드 id
    targets: list[str] = Field(default_factory=list)  # wrap: 감쌀 연속 형제 노드 id들
    anchor: str | None = None                 # insert/move: 기준 노드 id
    position: str | None = None               # before|after|into_start|into_end
    container: dict | None = None             # wrap: 새 컨테이너 스펙 {package, action, label?, parameters?}
    siblings_after: list[dict] = Field(default_factory=list)  # wrap: 컨테이너 뒤에 붙일 형제들(Catch/Finally/Else)
    action: dict | None = None                # insert: 새 액션 스펙
    parameters: list[dict] | None = None      # set_params: {name, value, value_source?} 목록
    package: str | None = None                # update
    action_name: str | None = None            # update (action 이름 — op 필드와 이름 충돌 회피)
    label: str | None = None                  # update
    notes: str | None = None                  # set_flow
    variables: list[dict] | None = None       # set_flow


class EditOps(BaseModel):
    """edit LLM의 최종 출력 — 연산 목록 + 사람용 요약/답변."""

    operations: list[EditOp] = Field(default_factory=list)
    change_summary: str = ""
    answer: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# id 부착 · 아웃라인 렌더 (프롬프트 입력용)
# ─────────────────────────────────────────────────────────────────────────────

def annotate_ids(flow: dict) -> dict:
    """흐름도의 모든 액션에 pre-order로 임시 id(n1, n2…)를 제자리에 붙인다.

    같은 구조면 항상 같은 id가 나오므로, 프롬프트에 보여준 id와 적용 대상 id가 일치한다.
    """
    counter = [0]

    def walk(actions: list[dict]) -> None:
        for a in actions:
            counter[0] += 1
            a[_ID] = f"n{counter[0]}"
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])
    return flow


def strip_ids(flow: dict) -> None:
    """전이 id를 모두 제거한다(스키마에 남기지 않는다)."""
    def walk(actions: list[dict]) -> None:
        for a in actions:
            a.pop(_ID, None)
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])


def renumber(flow: dict) -> None:
    """형제 그룹마다 order를 1부터 다시 매긴다 — 연산 적용으로 뒤틀린 순서를 정규화."""
    def walk(actions: list[dict]) -> None:
        for i, a in enumerate(actions):
            a["order"] = i + 1
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])


def render_outline(flow: dict) -> str:
    """id·패키지/액션·라벨·파라미터를 담은 계층 아웃라인 — LLM이 대상 id를 고르는 근거."""
    lines: list[str] = []

    def fmt_params(a: dict) -> str:
        ps = a.get("parameters") or []
        if not ps:
            return ""
        parts = [f"{p.get('name')}={p.get('value')!r}" for p in ps[:8]]
        return "  params: " + ", ".join(parts)

    def walk(actions: list[dict], depth: int) -> None:
        for a in actions:
            lines.append(
                "  " * depth
                + f"[{a.get(_ID)}] {a.get('package')}/{a.get('action')} «{a.get('label')}»"
                + fmt_params(a)
            )
            walk(a.get("children") or [], depth + 1)

    for step in flow.get("steps", []):
        lines.append(f"STEP {step.get('step_id')} :: {step.get('label') or ''}")
        walk(step.get("actions") or [], 1)
    return "\n".join(lines) or "(빈 흐름도)"


# ─────────────────────────────────────────────────────────────────────────────
# 연산 적용 (결정론)
# ─────────────────────────────────────────────────────────────────────────────

def _locate(flow: dict, node_id: str | None):
    """node_id를 가진 액션의 (형제_리스트, 인덱스)를 찾는다. 없으면 None."""
    if not node_id:
        return None

    def walk(actions: list[dict]):
        for i, a in enumerate(actions):
            if a.get(_ID) == node_id:
                return actions, i
            found = walk(a.get("children") or [])
            if found:
                return found
        return None

    for step in flow.get("steps", []):
        found = walk(step.get("actions") or [])
        if found:
            return found
    return None


def _new_action(spec: dict | None, children: list[dict] | None = None) -> dict:
    """LLM 스펙에서 새 액션 dict를 만든다 — order는 이후 renumber가 채운다."""
    spec = spec or {}
    return {
        "package": spec.get("package"),
        "action": spec.get("action"),
        "label": spec.get("label"),
        "parameters": spec.get("parameters") or [],
        "children": children if children is not None else (spec.get("children") or []),
    }


def _insert_at(parent: list[dict], node: dict, container: dict, position: str) -> bool:
    """anchor(=parent[?]) 기준 position에 node를 넣는다. parent는 anchor의 형제 리스트,
    container는 anchor 노드 자신(into_* 일 때 children 대상)."""
    if position in ("before", "after"):
        idx = parent.index(container)
        parent.insert(idx if position == "before" else idx + 1, node)
        return True
    if position == "into_start":
        container.setdefault("children", []).insert(0, node)
        return True
    if position == "into_end":
        container.setdefault("children", []).append(node)
        return True
    return False


def _apply_wrap(flow: dict, op: EditOp) -> bool:
    """연속한 형제 액션(targets)을 새 컨테이너의 children으로 감싼다(+siblings_after 추가)."""
    if not op.targets or not op.container:
        return False
    locs = [_locate(flow, t) for t in op.targets]
    if any(loc is None for loc in locs):
        return False
    parent = locs[0][0]
    if any(loc[0] is not parent for loc in locs):  # 같은 형제 리스트여야
        return False
    idxs = sorted(loc[1] for loc in locs)
    if idxs != list(range(idxs[0], idxs[0] + len(idxs))):  # 연속이어야
        return False
    nodes = [parent[i] for i in idxs]
    container = _new_action(op.container, children=nodes)
    for i in reversed(idxs):  # 뒤에서부터 제거해 인덱스 밀림 방지
        parent.pop(i)
    new_block = [container] + [_new_action(s) for s in (op.siblings_after or [])]
    parent[idxs[0]:idxs[0]] = new_block
    return True


def _apply_insert(flow: dict, op: EditOp) -> bool:
    loc = _locate(flow, op.anchor)
    if loc is None or not op.action:
        return False
    parent, idx = loc
    return _insert_at(parent, _new_action(op.action), parent[idx], op.position or "after")


def _apply_remove(flow: dict, op: EditOp) -> bool:
    loc = _locate(flow, op.target)
    if loc is None:
        return False
    parent, idx = loc
    parent.pop(idx)
    return True


def _apply_move(flow: dict, op: EditOp) -> bool:
    loc = _locate(flow, op.target)
    if loc is None:
        return False
    parent, idx = loc
    node = parent.pop(idx)
    aloc = _locate(flow, op.anchor)  # 제거 후 재탐색(인덱스 밀림 반영)
    if aloc is None:
        parent.insert(idx, node)  # 롤백
        return False
    aparent, aidx = aloc
    if not _insert_at(aparent, node, aparent[aidx], op.position or "after"):
        parent.insert(idx, node)  # 롤백
        return False
    return True


def _apply_set_params(flow: dict, op: EditOp) -> bool:
    loc = _locate(flow, op.target)
    if loc is None or op.parameters is None:
        return False
    parent, idx = loc
    node = parent[idx]
    by_name = {p.get("name"): p for p in (node.get("parameters") or [])}
    for p in op.parameters:
        name = p.get("name")
        if not name:
            continue
        by_name[name] = {
            "name": name, "value": p.get("value"),
            "value_source": p.get("value_source") or "llm",
        }
    node["parameters"] = list(by_name.values())
    return True


def _apply_update(flow: dict, op: EditOp) -> bool:
    loc = _locate(flow, op.target)
    if loc is None:
        return False
    node = loc[0][loc[1]]
    if op.package:
        node["package"] = op.package
    if op.action_name:
        node["action"] = op.action_name
    if op.label is not None:
        node["label"] = op.label
    return op.package is not None or op.action_name is not None or op.label is not None


def _apply_set_flow(flow: dict, op: EditOp) -> bool:
    changed = False
    if op.notes is not None:
        flow["notes"] = op.notes
        changed = True
    if op.variables is not None:
        flow["variables"] = op.variables
        changed = True
    return changed


_APPLIERS = {
    "wrap": _apply_wrap,
    "insert": _apply_insert,
    "remove": _apply_remove,
    "move": _apply_move,
    "set_params": _apply_set_params,
    "update": _apply_update,
    "set_flow": _apply_set_flow,
}


def apply_edit_ops(flow: dict, ops: list[EditOp]) -> tuple[int, list[str]]:
    """연산들을 순서대로 flow에 제자리 적용한다. (적용_수, 실패_사유들)을 반환한다.

    한 연산이 실패해도 나머지는 계속 시도한다 — 실패 사유는 재요청 피드백에 쓴다.
    호출 측이 이후 strip_ids/renumber로 정규화한다.
    """
    applied = 0
    errors: list[str] = []
    for i, op in enumerate(ops):
        try:
            ok = _APPLIERS[op.op](flow, op)
        except Exception as e:  # noqa: BLE001 — 한 연산 실패가 전체를 죽이지 않게
            errors.append(f"op[{i}] {op.op}: 오류 {e}")
            continue
        if ok:
            applied += 1
        else:
            errors.append(f"op[{i}] {op.op}: 대상 노드를 못 찾았거나 조건(연속 형제 등) 불충족")
    return applied, errors
