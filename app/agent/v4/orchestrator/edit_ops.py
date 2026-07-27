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
- update      : 노드의 package/action/label/parameters를 바꾼다. 표기가 바뀌면 새 스펙에 없는
                옛 파라미터를 함께 걷어낸다(_retarget_params) — 삭제 연산이 따로 없기 때문이다.
- set_flow    : 흐름도 수준 notes/variables와 스펙 전제(spec.assumptions)를 바꾼다.
- set_spec    : 채점 기준(spec)의 요구 목록·목표를 바꾼다 — "그 업무 자체가 필요 없다"는 수정용.

id는 프롬프트에 보여줄 때만 임시로 붙였다가(_annotate_ids) 적용 후 벗긴다(strip_ids) —
스키마(RecommendedAction)에는 저장하지 않는 관측용 필드다.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 임시 노드 id를 다는 전이(transient) 키 — 프롬프트 참조용, 적용 후 제거한다.
# harness도 같은 키로 노드를 훑어 '슬롯 목적' 블록을 만들기 때문에 공개 이름을 둔다
# (사설 이름을 모듈 밖에서 참조하면 이 키를 바꿀 때 조용히 깨진다).
NODE_ID = "_id"
_ID = NODE_ID

_POSITIONS = frozenset({"before", "after", "into_start", "into_end"})


def _coerce_spec(v):
    """액션 스펙 자리의 "패키지/액션" 문자열 슬립을 최소 dict로 코얼스한다.

    골드셋 베이스라인 평가에서 3회 실측(`operations.N.action: dict_type`) — 검증 거부는
    1회 재출력 후 교정 라운드 통째 폐기(현재본 유지)로 이어져 검수 위반이 미교정 출고된다.
    "Pkg/act" 꼴이면 최소 스펙 dict로, 패키지를 특정할 수 없는 문자열은 None으로 강등한다
    (해당 연산만 적용부에서 no-op — 배치 전체를 살린다). 그 외 타입은 그대로 반환해
    기존 검증에 맡긴다.
    """
    if isinstance(v, str):
        pkg, sep, act = v.strip().partition("/")
        if sep and pkg.strip() and act.strip():
            return {"package": pkg.strip(), "action": act.strip()}
        return None
    return v


class EditOp(BaseModel):
    """단일 수정 연산. op에 따라 쓰는 필드가 다르다(플랫 스키마 — LLM 출력 친화)."""

    op: Literal[
        "wrap", "insert", "remove", "move", "set_params", "update", "set_flow",
        "split_step", "merge_step", "set_spec",
    ]
    target: str | None = None                 # remove/move/set_params/update: 대상 노드 id
    targets: list[str] = Field(default_factory=list)  # wrap: 감쌀 연속 형제 노드 id들
    anchor: str | None = None                 # insert/move: 기준 노드 id
    position: str | None = None               # before|after|into_start|into_end
    container: dict | None = None             # wrap: 새 컨테이너 스펙 {package, action, label?, parameters?}
    siblings_after: list[dict] = Field(default_factory=list)  # wrap: 컨테이너 뒤에 붙일 형제들(Catch/Finally/Else)
    action: dict | None = None                # insert: 새 액션 스펙
    parameters: list[dict] | None = None      # set_params/update: {name, value, value_source?} 목록
    package: str | None = None                # update
    action_name: str | None = None            # update (action 이름 — op 필드와 이름 충돌 회피)
    label: str | None = None                  # update / split_step(새 단계 라벨)
    notes: str | None = None                  # set_flow
    variables: list[dict] | None = None       # set_flow
    assumptions: list[str] | None = None      # set_flow: 흐름도 전제 교체 (RPA-282)
    produces: list[dict] | None = None        # set_params/update: 변수 연결 동반 갱신 (v3)
    consumes: list[dict] | None = None        # set_params/update: 변수 연결 동반 갱신 (v3)
    step_id: str | None = None                # split_step/merge_step: 대상 단계
    # set_spec — 채점 기준(FlowSpec)의 요구를 조작한다 (§6.1). 액션만 지우고 요구를 남기면
    # 누락 blocker가 그 액션을 도로 불러온다 = 사용자의 삭제 지시가 검수에 의해 되돌려진다.
    remove_req_ids: list[str] = Field(default_factory=list)  # set_spec: 지울 요구 id들
    requirements: list[dict] | None = None    # set_spec: 요구 upsert ({req_id?, text, priority?, source?})
    goal: str | None = None                   # set_spec: 목표 문장 교체

    @field_validator("remove_req_ids", mode="before")
    @classmethod
    def _coerce_req_ids(cls, v):
        """단일 id 문자열("req-2") 슬립을 목록으로 승격 — 목록 강제 실패로 배치 전체가 죽지 않게."""
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v

    @field_validator("requirements", mode="before")
    @classmethod
    def _coerce_requirements(cls, v):
        """요구를 문자열로만 낸 슬립("메일 발송")을 최소 dict로 코얼스한다.

        req_id는 여기서 짓지 않는다 — 적용부가 기존 id와 충돌하지 않는 번호를 부여해야 한다.
        """
        if isinstance(v, list):
            return [
                {"text": item.strip()} if isinstance(item, str) else item
                for item in v
                if not isinstance(item, str) or item.strip()
            ]
        return v

    @field_validator("action", "container", mode="before")
    @classmethod
    def _coerce_action_spec(cls, v):
        """surgeon이 액션 스펙을 dict 대신 "패키지/액션" 문자열로 축약하는 슬립을 관대 수용 (_coerce_spec)."""
        return _coerce_spec(v)

    @field_validator("siblings_after", mode="before")
    @classmethod
    def _coerce_sibling_specs(cls, v):
        """siblings_after(wrap의 Catch/Finally/Else 목록)에도 같은 문자열 슬립 코얼스를 적용.

        코얼스 불가 항목(None 강등)은 목록에서 걷어내 나머지 형제들을 살린다.
        """
        if isinstance(v, list):
            out = [c for c in (_coerce_spec(item) for item in v) if c is not None]
            return out
        return v


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


def _var_refs(value) -> list[dict]:
    """produces/consumes 스펙을 VarRef dict 목록으로 정규화한다 (이름 없는 원소는 버린다)."""
    return [r for r in (value or []) if isinstance(r, dict) and r.get("name")]


def _new_action(spec: dict | None, children: list[dict] | None = None) -> dict:
    """LLM 스펙에서 새 액션 dict를 만든다 — order는 이후 renumber가 채운다."""
    spec = spec or {}
    out = {
        "package": spec.get("package"),
        "action": spec.get("action"),
        "label": spec.get("label"),
        "parameters": spec.get("parameters") or [],
        "children": children if children is not None else (spec.get("children") or []),
    }
    # 변수 연결(v3)은 삽입 시에도 보존한다 — 버리면 R9/R10이 새 액션을 못 본다.
    if spec.get("produces"):
        out["produces"] = _var_refs(spec.get("produces"))
    if spec.get("consumes"):
        out["consumes"] = _var_refs(spec.get("consumes"))
    # 담당 요구(req_id)도 보존한다 — 버리면 삽입으로 누락 blocker를 **영원히 못 지운다**.
    # 누락 판정이 "이 요구를 담당하는 액션이 있는가"라서, surgeon이 액션을 채워 넣어도
    # req_id가 떨어지면 여전히 미배정으로 잡혀 교정 루프가 예산만 태우고 자리표시자로 끝난다.
    if spec.get("req_id"):
        out["req_id"] = str(spec["req_id"])
    # 요구 앵커도 보존한다 — 누락(요구 미배정)을 메우려고 삽입한 액션에서 req_id를 버리면
    # 결정론 커버리지가 그 요구를 여전히 '미배정'으로 세고, 교정 루프가 같은 삽입을
    # 무한히 반복한다(수렴 실패).
    if spec.get("req_id"):
        out["req_id"] = spec["req_id"]
    return out


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


def _merge_params(node: dict, given: list[dict] | None, *, force_llm: bool = False) -> None:
    """given을 name 기준으로 node["parameters"]에 병합한다(있으면 치환, 없으면 뒤에 추가).

    set_params와 update가 **같은 함수**를 쓴다 — 갈라지면 같은 이름을 어느 연산으로 쓰느냐에
    따라 value_source 기본값이나 덮어쓰기 규칙이 달라진다.

    기존 항목의 나머지 키(label 등)를 보존한다. 이전 구현은 {name, value, value_source} 3키로
    **재구성**했는데 ActionParameter에는 label이 있어(schemas/recommendation.py), edit 경로가
    model_dump()한 흐름도를 넣으면 set_params 한 번에 사람용 라벨이 조용히 사라졌다.

    이름 없는 항목은 순서 그대로 둔다 — R2가 판정 대상에서 제외하는 것(checker: `if p.get("name")`)을
    여기서 지우거나 하나로 뭉개면 '걷어내는 집합 = R2가 보는 집합' 불변식이 깨진다.

    force_llm=True(update가 실어 온 값)면 value_source를 "llm"으로 고정한다. 사용자 입력은
    update로 오지 않는다 — LLM이 지어낸 값에 "user"가 붙으면 edit 경로의 _restore_user_values가
    교정 결과에 그 값을 **다시 고정**해 검수·교정이 영원히 못 건드리는 값이 된다.
    """
    if given is None:
        return
    current = list(node.get("parameters") or [])
    index = {
        p.get("name"): i
        for i, p in enumerate(current)
        if isinstance(p, dict) and p.get("name")
    }
    for p in given:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        source = "llm" if force_llm else (p.get("value_source") or "llm")
        merged = {"name": name, "value": p.get("value"), "value_source": source}
        i = index.get(name)
        if i is None:
            index[name] = len(current)
            current.append(merged)
        else:
            current[i] = {**current[i], **merged}  # label 등 기존 키 보존
    node["parameters"] = current


def _retarget_params(node: dict, allowed: frozenset[str] | None) -> list[str]:
    """표기를 갈아끼운 노드에서 **새 스펙에 없는** 파라미터를 걷어내고 걷어낸 이름을 돌려준다.

    ## 왜 필요한가 (실측, 2026-07-27)

    교정 5라운드 뒤 최종 가중합 210 중 **150이 R2 15건**이었고 전부 같은 원인이다: surgeon이
    `Step/stepAction`을 실제 액션으로 갈아끼웠는데 옛 자리의 `Title` 파라미터가 그대로 남았다.
    `set_params`는 병합이라 이름을 **지울 수 없고**, surgeon에게는 삭제 연산이 없다 — 어휘에
    없는 수리를 프롬프트로 요구하던 상태였다. 표기를 바꾼 쪽이 그 자리에서 정리한다.

    ## 왜 '전부 비우기'가 아닌가

    이름이 그대로 이어지는 파라미터(세션 패키지 교체 시의 `session`·`filePath`)까지 날아가고,
    그 자리에 required R3가 선다. required SESSION/SELECT/VARIABLE의 R3는 질문 카드가 아니라
    major finding이라 **가중치 이득이 0**이다 — 결함 하나를 다른 결함으로 바꾸는 것뿐이다.

    ## 왜 되돌릴 수 없는가

    `value_source="user"` 값도 함께 지워질 수 있는데 복구 경로가 없다. edit 경로의
    `_restore_user_values`가 읽는 `collect()`는 **이미 정리된 흐름도**를 훑으므로, 지운 값은
    애초에 저장 대상에 들어가지 않는다. 그래서 호출 측이 `allowed`를 줄지(=정리할지)를
    카탈로그 신뢰도로 게이팅한다 — harness.spec_param_lookup 참조.

    allowed가 None이면 아무것도 안 한다: 스펙 부재·params_unknown은 R2가 침묵하는 자리라
    걷어내는 쪽도 침묵해야 두 판정이 어긋나지 않는다.
    """
    if allowed is None:
        return []
    kept: list = []
    dropped: list[str] = []
    for p in node.get("parameters") or []:
        if not isinstance(p, dict):
            kept.append(p)
            continue
        name = p.get("name")
        if not name or name in allowed:  # 이름 없는 항목은 R2가 안 보므로 여기서도 안 건드린다
            kept.append(p)
        else:
            dropped.append(name)
    if dropped:
        node["parameters"] = kept
    return dropped


def _apply_set_params(flow: dict, op: EditOp) -> bool:
    loc = _locate(flow, op.target)
    if loc is None or (op.parameters is None and op.produces is None and op.consumes is None):
        return False
    parent, idx = loc
    node = parent[idx]
    _merge_params(node, op.parameters)
    # 변수 연결(v3) 동반 갱신 — 파라미터가 바뀌면 $var$ 연결도 함께 바뀌는 경우가 잦다.
    if op.produces is not None:
        node["produces"] = [r for r in op.produces if isinstance(r, dict) and r.get("name")]
    if op.consumes is not None:
        node["consumes"] = [r for r in op.consumes if isinstance(r, dict) and r.get("name")]
    return True


def _apply_update(
    flow: dict,
    op: EditOp,
    *,
    spec_params=None,
    prune_log: list[dict] | None = None,
) -> bool:
    """노드의 표기·라벨·변수·파라미터를 갱신한다. 표기가 바뀌면 옛 파라미터를 정리한다.

    순서가 중요하다: 표기 → 파라미터 병합 → 새 스펙 기준 정리. 병합을 먼저 해야 surgeon이
    새 액션용으로 실어 보낸 값이 남고, 정리를 나중에 해야 **새** 표기의 스펙으로 판정된다.

    `spec_params(package, action) -> frozenset[str] | None`을 주입받는다 — 이 모듈이 카탈로그
    타입을 몰라도 되게(drop_unknown_action_ops의 `exists`와 같은 규약). 안 주면 정리하지
    않는다: 스펙을 모르는 상태의 삭제는 '모름 → 침묵' 원칙 위반이다.
    """
    loc = _locate(flow, op.target)
    if loc is None:
        return False
    node = loc[0][loc[1]]
    before = (node.get("package"), node.get("action"))
    if op.package:
        node["package"] = op.package
    if op.action_name:
        node["action"] = op.action_name
    if op.label is not None:
        node["label"] = op.label
    if op.produces is not None:
        node["produces"] = _var_refs(op.produces)
    if op.consumes is not None:
        node["consumes"] = _var_refs(op.consumes)
    # update가 실은 값은 LLM 산출이다 — value_source를 강제해 사용자 값으로 위장되지 않게.
    _merge_params(node, op.parameters, force_llm=True)

    after = (node.get("package"), node.get("action"))
    if spec_params is not None and after != before and all(after):
        dropped = _retarget_params(node, spec_params(*after))
        if dropped and prune_log is not None:
            prune_log.append({
                "node": op.target, "to": f"{after[0]}/{after[1]}", "dropped": dropped[:6],
            })
    return any(
        v is not None
        for v in (op.package, op.action_name, op.label, op.produces, op.consumes, op.parameters)
    )


def _ensure_spec(flow: dict) -> dict:
    """flow["spec"](채점 기준 FlowSpec)을 쓰기 가능한 dict로 정규화해 돌려준다.

    setdefault를 쓰면 안 된다: Recommendation.spec의 기본값이 None이라 model_dump()를 거친
    흐름도는 spec 키가 **None 값으로 존재**하고, setdefault는 키가 있으면 값을 바꾸지 않는다.
    그러면 갱신이 조용히 유실될 뿐 아니라 changed가 False로 남아 연산이 '적용 실패'로 기록돼
    사용자에게 "반영하지 못했어요"가 나간다 (RPA-282에서 실제로 겪은 버그).

    ⚠ 호출 측은 '실제로 바꿀 것이 확정된 뒤에만' 부른다 — 무효 연산에서도 부르면 spec이
    None이던 흐름도에 빈 dict가 생겨 무변경 판정이 흔들린다.
    """
    spec = flow.get("spec")
    if not isinstance(spec, dict):  # None·구버전 잔재·타입 슬립을 모두 정규화
        flow["spec"] = spec = {}
    return spec


def spec_requirements(flow: dict) -> list[dict]:
    """흐름도에 동봉된 요구 목록(spec.requirements). 없으면 빈 목록.

    수정 전후 비교(무변경 판정)에서도 쓰라고 공개한다 — 요구만 바뀐 편집을 '무변경'으로
    저하시키면 사용자가 지운 업무가 되살아난다.
    """
    spec = flow.get("spec")
    return list(spec.get("requirements") or []) if isinstance(spec, dict) else []


def _apply_set_flow(flow: dict, op: EditOp) -> bool:
    changed = False
    if op.notes is not None:
        flow["notes"] = op.notes
        changed = True
    if op.variables is not None:
        flow["variables"] = op.variables
        changed = True
    if op.assumptions is not None:
        # 전제는 흐름도가 아니라 채점 기준(FlowSpec)에 산다 — recommend가 flow["spec"]으로
        # 동봉해 두고(graph.py finalize), 이후 턴의 재채점·재생성이 그걸 다시 읽는다.
        # 여기서 갱신해야 "대상 OS를 바꿔 달라"는 요청이 다음 턴까지 살아남는다 (RPA-282).
        _ensure_spec(flow)["assumptions"] = op.assumptions
        changed = True
    return changed


def _next_req_id(taken: set[str]) -> str:
    """기존 id와 충돌하지 않는 다음 req-N을 고른다 — req_id는 L2 채점·심판·카드의 공유 앵커라
    중복되면 서로 다른 요구가 한 칸으로 뭉개진다 (spec.py의 보정 규칙과 같은 방식)."""
    n = 1
    while f"req-{n}" in taken:
        n += 1
    return f"req-{n}"


def _detach_req_ids(flow: dict, removed: set[str]) -> None:
    """지워진 요구를 가리키던 액션의 req_id를 떼어 낸다.

    요구를 지우면서 그 요구를 담당하던 액션 전부를 함께 지우는 것은 아니다(일부만 남길 수
    있다). 남은 액션이 사라진 요구를 계속 가리키면 커버리지 채점과 surgeon 슬롯 목적이
    존재하지 않는 앵커를 참조한다 — 매달린 참조를 여기서 끊어 상태를 일관되게 둔다.
    req_id 필드가 아직 없는 흐름도에서는 자연히 no-op이다.
    """
    def walk(actions: list[dict]) -> None:
        for a in actions:
            if a.get("req_id") in removed:
                a["req_id"] = None
            walk(a.get("children") or [])

    for step in flow.get("steps") or []:
        walk(step.get("actions") or [])


def _apply_set_spec(flow: dict, op: EditOp) -> bool:
    """채점 기준(spec)의 요구 목록·목표를 바꾼다 (설계 §6.1).

    왜 이 연산이 필요한가: 검수는 spec의 요구를 기준으로 '누락'을 판정한다. 사용자가
    "이 메일 발송 단계 빼주세요"라고 해서 액션만 remove하면 요구는 그대로 남아 누락
    blocker(가중치 100)가 발화하고, 교정 루프가 그 액션을 **도로 넣는다** — 사용자의 지시가
    검수에 의해 조용히 되돌려진다. 액션을 빼는 이유가 "그 업무가 필요 없다"면 요구도 함께
    지워야 상태가 일관된다(요구가 없으니 누락도 없다).

    remove_req_ids(삭제) → requirements(upsert) → goal 순으로 적용한다. 아무것도 실제로
    바뀌지 않으면 False를 돌려 '미적용'으로 보고한다 — 존재하지 않는 req_id를 지우라는
    연산을 성공으로 삼키면 사용자는 지워진 줄 안다.
    """
    cur = flow.get("spec")
    reqs = list((cur or {}).get("requirements") or []) if isinstance(cur, dict) else []
    changed_reqs = False

    if op.remove_req_ids:
        drop = {rid for rid in op.remove_req_ids if rid}
        kept = [r for r in reqs if r.get("req_id") not in drop]
        if len(kept) != len(reqs):
            _detach_req_ids(flow, drop)
            reqs = kept
            changed_reqs = True

    if op.requirements:
        by_id = {r.get("req_id"): i for i, r in enumerate(reqs) if r.get("req_id")}
        for item in op.requirements:
            if not isinstance(item, dict):
                continue
            rid = item.get("req_id")
            if rid and rid in by_id:  # 기존 요구 수정 — 준 필드만 덮어쓴다(부분 갱신)
                before = reqs[by_id[rid]]
                after = {**before, **{k: v for k, v in item.items() if v is not None}}
                if after != before:
                    reqs[by_id[rid]] = after
                    changed_reqs = True
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue  # 본문 없는 요구는 채점 앵커가 못 된다 — 조용히 버린다
            rid = rid or _next_req_id({r.get("req_id") for r in reqs if r.get("req_id")})
            reqs.append({
                "req_id": rid, "text": text,
                "priority": item.get("priority") or "must",
                "source": item.get("source") or "chat",
            })
            by_id[rid] = len(reqs) - 1
            changed_reqs = True

    goal_changed = op.goal is not None and (cur or {}).get("goal") != op.goal
    if not changed_reqs and not goal_changed:
        return False

    spec = _ensure_spec(flow)  # 실변경이 확정된 뒤에만 실체화
    if changed_reqs:
        spec["requirements"] = reqs
    if goal_changed:
        spec["goal"] = op.goal
    return True


def _find_step(flow: dict, step_id: str | None) -> int | None:
    if not step_id:
        return None
    for i, s in enumerate(flow.get("steps") or []):
        if s.get("step_id") == step_id:
            return i
    return None


def _apply_split_step(flow: dict, op: EditOp) -> bool:
    """단계를 둘로 쪼갠다 — at(top-level 노드 id)부터 끝까지를 새 단계로 옮긴다 (v3).

    refine이 심판의 구조 이식 지시를 실행할 때 단계 재구성이 필요해 추가됐다.
    새 step_id는 '<원래 id>-b'로 결정론 부여한다 (id 충돌 시 접미 반복).
    """
    si = _find_step(flow, op.step_id)
    if si is None or not op.anchor:
        return False
    step = flow["steps"][si]
    actions = step.get("actions") or []
    at = next((i for i, a in enumerate(actions) if a.get(_ID) == op.anchor), None)
    if at is None or at == 0:  # 첫 액션에서 쪼개면 원 단계가 비어버린다 — 무효
        return False
    existing = {s.get("step_id") for s in flow.get("steps") or []}
    new_id = f"{step.get('step_id')}-b"
    while new_id in existing:
        new_id += "b"
    new_step = {
        "step_id": new_id,
        "label": op.label or f"{step.get('label') or step.get('step_id')} (분리)",
        "description": None,
        "actions": actions[at:],
    }
    step["actions"] = actions[:at]
    flow["steps"].insert(si + 1, new_step)
    return True


def _apply_merge_step(flow: dict, op: EditOp) -> bool:
    """단계를 직전 단계에 합친다 — actions를 이어붙이고 이 단계를 제거한다 (v3)."""
    si = _find_step(flow, op.step_id)
    if si is None or si == 0:  # 첫 단계는 합칠 앞 단계가 없다
        return False
    prev, cur = flow["steps"][si - 1], flow["steps"][si]
    prev.setdefault("actions", []).extend(cur.get("actions") or [])
    flow["steps"].pop(si)
    return True


_APPLIERS = {
    "wrap": _apply_wrap,
    "insert": _apply_insert,
    "remove": _apply_remove,
    "move": _apply_move,
    "set_params": _apply_set_params,
    "update": _apply_update,
    "set_flow": _apply_set_flow,
    "set_spec": _apply_set_spec,
    "split_step": _apply_split_step,
    "merge_step": _apply_merge_step,
}


def _spec_notation(spec) -> tuple[str, str] | None:
    """액션 스펙 dict에서 (package, action) 표기를 꺼낸다 — 둘 다 있어야 판정할 수 있다."""
    if not isinstance(spec, dict):
        return None
    pkg, act = spec.get("package"), spec.get("action")
    return (pkg, act) if pkg and act else None


def op_notations(
    flow: dict,
    op: EditOp,
    projected: dict[str, tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """이 연산이 흐름도에 **새로 써 넣을** (package, action) 표기들.

    `update`는 package/action 중 한쪽만 줄 수 있어(둘 다 선택 필드) 대상 노드의 현재 값과
    합쳐야 결과 표기가 나온다 — 예: package만 바꾸면 action은 그대로 남는다. 그래서 노드를
    찾아본다. 못 찾으면 어차피 적용도 실패하므로 판정할 것이 없다(빈 목록).

    `projected`는 **앞선 연산이 이미 바꿔 놓을** 표기다(target → (package, action)). 사전
    검증은 아무 연산도 적용되기 전 흐름도로 판정하므로, 같은 노드에 update가 둘 이상 오면
    뒤의 판정이 앞의 효과를 못 본다. 그 결과가 예전에는 '연산 하나를 헛되이 버림'이었지만,
    이제 update가 **판정된 표기 기준으로 파라미터를 지우므로** 어긋나면 파괴적이다.

    표기를 안 쓰는 연산(remove/move/set_params/set_flow/…)은 빈 목록이다.
    """
    if op.op == "update":
        if not (op.package or op.action_name):
            return []  # 라벨·변수·파라미터만 바꾸는 update — 표기를 건드리지 않는다
        base = (projected or {}).get(op.target or "")
        if base is None:
            loc = _locate(flow, op.target)
            if loc is None:
                return []
            node = loc[0][loc[1]]
            base = (node.get("package"), node.get("action"))
        pkg = op.package or base[0]
        act = op.action_name or base[1]
        return [(pkg, act)] if pkg and act else []
    if op.op == "insert":
        n = _spec_notation(op.action)
        return [n] if n else []
    if op.op == "wrap":
        out = []
        for spec in [op.container, *op.siblings_after]:
            n = _spec_notation(spec)
            if n:
                out.append(n)
        return out
    return []


def drop_unknown_action_ops(
    flow: dict,
    ops: list[EditOp],
    exists,
    *,
    banned_out: list[str] | None = None,
) -> tuple[list[EditOp], list[str]]:
    """카탈로그에 없는 표기를 써 넣는 연산을 **적용 전에** 걸러낸다 (RPA-298).

    ## 왜 필요한가 (실측, 2026-07-27)

    surgeon이 연산 7개를 냈고 7개 다 적용됐다. 그중 하나가 `Excel advanced/Paste cell`인데
    그 액션은 **없다**(붙여넣기는 Microsoft 365 Excel·Google Sheets에만 있다). 재검수에서
    R1(blocker, 100점)이 발화해 가중합이 526→556으로 **악화**했고, 회귀 가드가 패치를
    통째로 폐기했다 — R17을 실제로 고친 연산까지 **좋은 6개가 나쁜 1개에 끌려 죽었다**.

    R1이 하는 것과 **같은 조회**를 사후가 아니라 사전에 한다. 환각 자체를 막지는 못한다
    (surgeon은 여전히 없는 표기를 제안할 것이다) — 막는 것은 그 하나가 나머지를 죽이는
    구조다.

    ## 딸린 연산도 함께 버린다

    surgeon은 표기 교체와 파라미터 설정을 **짝으로** 낸다:

        update     n8 → Excel advanced/Paste cell
        set_params n8 [Source cell selection, Destination cell]

    앞을 버리고 뒤만 남기면 **바뀌지 않은 액션에 다른 액션의 파라미터를 꽂아** R2를 새로
    만든다. 그래서 버린 연산의 target을 뒤에서 다시 건드리는 연산도 같이 버린다.
    `anchor`(insert 기준점)는 대상이 그대로 남아 있어 무효가 되지 않으므로 건드리지 않는다.

    `exists(package, action) -> bool`을 주입받는다 — 이 모듈이 카탈로그 타입을 몰라도 되게.

    `banned_out` 목록을 주면 실재하지 않던 표기를 "패키지/액션" 문자열로 **연산 순서대로**
    덧붙인다(이미 들어 있으면 건너뛴다). 호출부가 따로 계산하면 여기의 순차 투영을 다시
    구현해야 하고, 그러면 두 판정이 어긋나 프롬프트가 "쓰지 마라"고 말하지 않은 표기를
    실제로는 버리게 된다. set으로 모으면 순회 순서가 프로세스마다 달라져 **프롬프트 본문이
    비결정론이 된다**(절단 대상이 바뀐다) — 그래서 목록이다.
    """
    kept: list[EditOp] = []
    dropped: list[str] = []
    poisoned: set[str] = set()
    # 살아남은 update가 만들 표기를 순차로 투영한다 — 같은 노드를 두 번 건드릴 때 뒤의 판정이
    # 앞의 효과를 보게(op_notations 독스트링). 버려진 연산은 반영하지 않는다(적용되지 않는다).
    projected: dict[str, tuple[str, str]] = {}

    for i, op in enumerate(ops or []):
        if op.target and op.target in poisoned:
            dropped.append(f"op[{i}] {op.op}: 앞서 버린 연산과 같은 대상({op.target})이라 함께 제외")
            continue
        notations = op_notations(flow, op, projected)
        bad = [(p, a) for p, a in notations if not exists(p, a)]
        if bad:
            dropped.append(
                f"op[{i}] {op.op}: 카탈로그에 없는 표기 {', '.join(f'{p}/{a}' for p, a in bad)}"
            )
            if banned_out is not None:
                for p, a in bad:
                    if f"{p}/{a}" not in banned_out:
                        banned_out.append(f"{p}/{a}")
            for t in [op.target, *op.targets]:
                if t:
                    poisoned.add(t)
            continue
        if op.op == "update" and op.target and notations:
            projected[op.target] = notations[0]
        kept.append(op)
    return kept, dropped


def apply_edit_ops(
    flow: dict,
    ops: list[EditOp],
    *,
    spec_params=None,
    prune_log: list[dict] | None = None,
) -> tuple[int, list[str]]:
    """연산들을 순서대로 flow에 제자리 적용한다. (적용_수, 실패_사유들)을 반환한다.

    한 연산이 실패해도 나머지는 계속 시도한다 — 실패 사유는 재요청 피드백에 쓴다.
    호출 측이 이후 strip_ids/renumber로 정규화한다.

    `spec_params(package, action) -> frozenset[str] | None`을 주면 update가 표기를 갈아끼울 때
    새 스펙에 없는 옛 파라미터를 정리한다(_retarget_params). 정리 기록은 `prune_log`에 쌓이며
    **errors가 아니다** — errors에 실으면 edit 경로가 정상 정리를 '미완결'로 보고 사용자에게
    "반영하지 못했어요"를 내보낸다(edit.py의 _CANT_APPLY 분기).
    """
    applied = 0
    errors: list[str] = []
    for i, op in enumerate(ops):
        try:
            ok = (
                _apply_update(flow, op, spec_params=spec_params, prune_log=prune_log)
                if op.op == "update"
                else _APPLIERS[op.op](flow, op)
            )
        except Exception as e:  # noqa: BLE001 — 한 연산 실패가 전체를 죽이지 않게
            errors.append(f"op[{i}] {op.op}: 오류 {e}")
            continue
        if ok:
            applied += 1
        elif op.op in ("set_flow", "set_spec"):
            # 노드를 안 쓰는 연산이라 "대상 노드를 못 찾았다"는 안내가 오히려 오도한다 —
            # 재요청 피드백이 엉뚱한 곳을 고치게 만든다.
            errors.append(
                f"op[{i}] {op.op}: 바뀐 값이 없다(빈 필드이거나, 지우려는 req_id가 스펙에 없음)"
            )
        else:
            errors.append(f"op[{i}] {op.op}: 대상 노드를 못 찾았거나 조건(연속 형제 등) 불충족")
    return applied, errors
