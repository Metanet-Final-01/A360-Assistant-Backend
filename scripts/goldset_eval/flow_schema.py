# -*- coding: utf-8 -*-
"""정답 봇 JSON → **우리 흐름도 스키마**로 변환하고 KB 어휘로 사상한다 (RPA-298 Phase 0-1).

기존 `gold.py`는 봇 JSON을 **평평한 (package, action) 시퀀스**로 접는다. 중첩(Loop/If/
Error handler의 본문)과 파라미터가 통째로 버려지므로, 채점이 "액션 이름이 비슷한가"밖에
못 본다. 그래서 `notation.py`의 토큰 유사도(MATCH_THRESHOLD=0.55) 퍼지 매칭이 필요했다.

이 모듈은 정답 봇을 `app/schemas/recommendation.py`의
`Recommendation`/`StepRecommendation`/`RecommendedAction`과 **같은 모양**으로 옮긴다:

    Recommendation.steps[].actions[].children[]   ← 컨테이너 중첩 그대로
    RecommendedAction.parameters[]                ← 봇 attributes 그대로

이렇게 옮겨 두면 채점기가 **퍼지 유사도 없이** 이름 일치·중첩 경로·순서를 직접 비교할 수
있다(`exact_metrics.py`). 다만 표기 체계가 다르므로(봇 `Folder/createFolder` ↔ KB
`Folder/Create`) 한 번은 접어야 하는데, **매 채점마다 퍼지로 다시 푸는 대신 빌드 시 한 번
풀어 파일로 박아 둔다** — 그게 "고정된 정답지"의 뜻이다. 사상 결과에는 근거(via/sim/동률
후보)가 같이 남아 사람이 감사·교정할 수 있다.

⚠️ 기존 `gold.py`/`metrics.py`/`notation.py`의 채점 축은 **건드리지 않는다.** 지난 모든
기준선 런이 그 축으로 재졌기 때문에 지우면 비교가 불가능해진다. 이 모듈이 만드는 것은
나란히 놓이는 **새 축**의 원료다.

## 변환 규약 (왜 이렇게 접는가)

- **branches → 형제로 승격.** 봇 JSON은 `If/if`의 else, `ErrorHandler/try`의 catch·finally를
  `branches[]`에 담지만, `RecommendedAction`은 `children[]`만 있고 분기를 **다음 형제**로
  표현한다(스키마 docstring의 "If → Else → 다음 형제 = 병합 지점"). 그 규약에 맞춘다.
  평탄화 순서는 `gold._walk`(노드 → children → branches)와 정확히 같아져서, 변환 트리를
  평탄화하면 `gold.merged_sequence`와 일치한다 — 두 축이 같은 액션 모집단을 본다는 보증이다.
- **최상위 `Step/step` → `steps[]`, 중첩 `Step/step` → 컨테이너 액션.** 실측상 Step은 깊이
  8까지 중첩되는데(23/21/29/7/2/3건) `Recommendation.steps[]`는 평평해서 다 담을 수 없다.
  최상위만 단계로 올리고 나머지는 컨테이너 액션으로 남긴다.
- **`Comment/Comment` 제외.** 실행 의미가 없고 골드셋에 134회 나와 지표를 오염시킨다.
  `gold.SCAFFOLD`와 같은 판단이며, 몇 개를 뺐는지는 `stats.comments`에 남긴다.
- **`disabled` 노드와 그 하위 전체 제외.** 실행되지 않으므로 정답이 아니다(`gold`와 동일).
- **자격증명 마스킹.** 정답 봇에 평문 자격증명이 들어 있다(실측: `Rest/restPost`의
  `customHeaders`에 `Basic <base64(email:password)>`). 산출물은 새로 커밋되는 파일이므로
  그대로 옮기면 비밀을 한 벌 더 퍼뜨리는 셈이다. 이름·값 패턴으로 가린다.
"""

import base64
import hashlib
import json
import re
from pathlib import Path

from .gold import BOILERPLATE_STEP_TITLES
from .notation import (
    CONTAINER_PKG_KEYS,
    MATCH_THRESHOLD,
    CanonAction,
    canon_package,
)

# 실행 의미가 없어 변환 트리에서 아예 빼는 노드 (gold.SCAFFOLD와 같은 판단).
# Step은 여기 없다 — 구조를 나르므로 단계/컨테이너로 남기고, 액션 지표에서만 제외한다.
_DROP = {("Comment", "Comment")}

# 파라미터 문자열 절단 길이. 정답지는 사람이 열어 보는 감사 자산인데, UIOBJECT blob이나
# 긴 HTML/SQL이 섞이면 파일이 수 MB로 부풀어 열리지 않는다. 비교에 쓰는 건 이름·타입이고
# 값은 근거 확인용이라 앞부분만 있으면 충분하다.
_MAX_VALUE_CHARS = 400

# 값이 비밀일 가능성이 높은 파라미터 이름 (부분 일치, 대소문자 무시)
_SECRET_NAME_RE = re.compile(
    r"password|passwd|secret|apikey|api_key|authorization|token|credential|privatekey",
    re.I,
)
# 값 자체가 비밀 형태인 경우 — 이름이 무해해도(customHeaders 안의 value) 걸러야 한다
_SECRET_VALUE_RE = re.compile(r"\b(Basic|Bearer)\s+[A-Za-z0-9+/=_\-.]{16,}")

REDACTED = "«redacted»"


# ── 컨테이너 판별 ────────────────────────────────────────────────────────────
#
# 봇 표기(`ErrorHandler/try`)와 KB 표기(`Error handler/Try`)를 같은 정준 종류로 접는다.
# 대소문자·구분자 접기만 쓰고 토큰 유사도는 쓰지 않는다 — 이 축의 계약이 "퍼지 없음"이다.
_CONTAINER_ACTION_KINDS = {
    "errorhandler": {"try": "try", "catch": "catch", "finally": "finally"},
    "step": {"step": "step"},
}

_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _action_tokens(action: str) -> list[str]:
    """액션명을 소문자 토큰으로. camelCase(`elseIf`)와 구분자(`else if (optional)`) 둘 다 쪼갠다."""
    out: list[str] = []
    for chunk in re.split(r"[^0-9A-Za-z]+", action or ""):
        if chunk:
            out.extend(t.lower() for t in _CAMEL_SPLIT.split(chunk) if t)
    return out


def container_kind(package: str, action: str) -> str | None:
    """(package, action) → 정준 컨테이너 종류. 컨테이너가 아니면 None.

    토큰 **포함** 여부로 판정하는 이유: 에이전트가 `Error handler/Error handler/Try`처럼
    패키지명을 액션에 겹쳐 내거나 KB가 `Else if (optional)`처럼 괄호를 붙인 실측 사례가 있다.
    그건 표기 사고지 다른 구조가 아니므로 **구조** 지표에서는 같게 봐야 한다
    (액션 이름 정확도 지표에서는 여전히 불일치로 잡힌다 — 거기선 `exact_key`를 쓴다).
    """
    pkg = canon_package(package)
    toks = _action_tokens(action)
    if pkg == "loop":
        # Break/Continue는 흐름 제어지 컨테이너가 아니다.
        if toks and toks[-1] in ("break", "continue"):
            return None
        return "loop"
    if pkg == "triggerloop":
        return "loop"
    if pkg == "if":
        # "else if"는 두 토큰으로 쪼개지므로 else/if 단독보다 **먼저** 봐야 한다.
        if "elseif" in toks or ("else" in toks and "if" in toks):
            return "elseif"
        if "else" in toks:
            return "else"
        if "if" in toks:
            return "if"
        return None
    table = _CONTAINER_ACTION_KINDS.get(pkg)
    if not table or not toks:
        return None
    for t in reversed(toks):
        if t in table:
            return table[t]
    return table.get("".join(toks))


def is_scaffold_action(package: str, action: str) -> bool:
    """액션 지표 모집단에서 빼는 순수 구획(Step)·주석(Comment).

    `notation.is_scaffold`와 같은 판정이지만 이 모듈 안에서 자족적으로 쓰려고 다시 둔다
    (두 축이 서로의 내부 구현에 얽히지 않게).
    """
    return canon_package(package) in ("step", "comment")


# ── 파라미터 추출 ────────────────────────────────────────────────────────────


def _digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]


def _ui_object_summary(value: dict) -> dict:
    """UIOBJECT를 사람이 읽을 수 있는 최소 정보로 접는다.

    원본은 base64 blob(수 KB)이라 그대로 실으면 정답지가 수 MB가 된다. blob은 UI 객체
    JSON이므로 이름·창 제목·DOM XPath만 뽑고, 실패하면 지문(digest)만 남긴다 — 지문만
    있어도 "같은 UI 객체를 가리켰나"는 판정된다.
    """
    out: dict = {"type": "UIOBJECT", "digest": _digest(value)}
    blob = (value.get("uiObject") or {}).get("blob") if isinstance(value.get("uiObject"), dict) else None
    if not isinstance(blob, str):
        return out
    try:
        node = json.loads(base64.b64decode(blob).decode("utf-8")).get("objNode") or {}
        if node.get("name"):
            out["name"] = str(node["name"])[:80]
        if node.get("windowTitle"):
            out["window"] = str(node["windowTitle"])[:120]
        for prop in node.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name") == "DOMXPath" and prop.get("value"):
                out["xpath"] = str(prop["value"])[:160]
                break
    except Exception:  # noqa: BLE001 — 외부 데이터, 못 읽으면 지문만으로 충분하다
        pass
    return out


def _scrub(name: str, text: str) -> str:
    """비밀로 보이는 값을 가린다. `$var$` 보간은 비밀이 아니므로 그대로 둔다."""
    if not text:
        return text
    if text.startswith("$") and text.endswith("$"):
        return text
    if _SECRET_NAME_RE.search(name or ""):
        return REDACTED
    return _SECRET_VALUE_RE.sub(REDACTED, text)


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "…"


def _attr_value(name: str, value) -> object:
    """봇 attribute value dict → 비교 가능한 스칼라/요약.

    타입별로 무엇이 의미인지가 다르다: CONDITIONAL은 조건 종류(`Folder/folderDoesNotExists`),
    ITERATOR는 반복자 종류(`Excel_MS/loop.iterators.excel`)가 곧 그 컨테이너가 무슨 일을
    하는지다 — 컨테이너 사상(`resolve_action`)이 이 값을 근거로 쓴다.
    """
    if not isinstance(value, dict):
        return _scrub(name, _clip(str(value))) if isinstance(value, str) else value
    vtype = value.get("type")
    if vtype == "UIOBJECT":
        return _ui_object_summary(value)
    if vtype == "ITERATOR":
        return f"{value.get('packageName')}/{value.get('iteratorName')}"
    if vtype == "CONDITIONAL":
        return f"{value.get('packageName')}/{value.get('conditionalName')}"
    if vtype == "EXCEPTION":
        return f"{value.get('packageName')}/{value.get('exceptionName')}"
    if vtype == "VARIABLE":
        return f"${value.get('variableName')}$"
    if vtype == "SESSION":
        inner = value.get("sessionName")
        if isinstance(inner, dict):
            return _scrub(name, _clip(str(inner.get("string") or "")))
        return _scrub(name, _clip(str(inner or "")))
    if vtype == "BOOLEAN":
        return value.get("boolean")
    if vtype == "NUMBER":
        n = value.get("number")
        return n if n is not None else _scrub(name, _clip(str(value.get("expression") or "")))
    if vtype == "LIST":
        return [_attr_value(name, v) for v in (value.get("list") or [])]
    if vtype == "DICTIONARY":
        out = {}
        for ent in value.get("dictionary") or []:
            if isinstance(ent, dict) and ent.get("key") is not None:
                out[str(ent["key"])] = _attr_value(str(ent["key"]), ent.get("value"))
        # 헤더 리스트처럼 {name: "Authorization", value: "Basic ..."} 꼴이면 name을 근거로
        # value를 가린다 — 키 하나만 보면 "value"라 무해해 보이기 때문이다.
        if _SECRET_NAME_RE.search(str(out.get("name") or "")) and "value" in out:
            out["value"] = REDACTED
        return out
    text = value.get("string")
    if text is None:
        text = value.get("expression")
    if text is None:
        return None
    return _scrub(name, _clip(str(text)))


def _parameters(node: dict) -> list[dict]:
    """봇 attributes → `ActionParameter` 모양(name/value)의 목록.

    `value_source`는 두지 않는다 — 정답 봇의 값은 전부 사람이 넣은 것이라 구분이 없다.
    대신 원 타입(`type`)을 남겨 파라미터 종류 비교가 가능하게 한다.
    """
    out: list[dict] = []
    for attr in node.get("attributes") or []:
        if not isinstance(attr, dict) or not attr.get("name"):
            continue
        name = str(attr["name"])
        raw = attr.get("value")
        vtype = raw.get("type") if isinstance(raw, dict) else None
        out.append({"name": name, "type": vtype, "value": _attr_value(name, raw)})
    return out


def _step_title(node: dict) -> str | None:
    for attr in node.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("name") == "title":
            val = attr.get("value")
            if isinstance(val, dict) and isinstance(val.get("string"), str):
                return val["string"].strip()
    return None


def _produces(node: dict) -> list[dict]:
    """`returnTo`(단일) + `returns`(복수) → `VarRef` 모양.

    `returns`는 리스트가 아니라 **dict**다(실측: `ErrorHandler/catch`의
    `{"errorLineNumber": {...}, "errorMessage": {...}}` 12건). 리스트로 착각해 순회하면
    키 문자열만 돌아 출력 변수가 통째로 사라진다 — role에 그 키를 담아 무엇을 받는
    변수인지도 남긴다.
    """
    out: list[dict] = []
    rt = node.get("returnTo")
    if isinstance(rt, dict) and rt.get("variableName"):
        out.append({"name": str(rt["variableName"])})
    returns = node.get("returns")
    if isinstance(returns, dict):
        for role, ref in returns.items():
            if isinstance(ref, dict) and ref.get("variableName"):
                out.append({"name": str(ref["variableName"]), "role": str(role)})
    elif isinstance(returns, list):  # 다른 export 세대 대비
        for ref in returns:
            if isinstance(ref, dict) and ref.get("variableName"):
                out.append({"name": str(ref["variableName"])})
    return out


_VAR_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_\- ]*)(?:\.[^$]*)?\$")


def _consumes(params: list[dict]) -> list[dict]:
    """파라미터 문자열의 `$var$` 보간 → 소비 변수. 중복 제거·등장 순서 유지."""
    seen: list[str] = []
    def scan(v):
        if isinstance(v, str):
            for m in _VAR_REF_RE.finditer(v):
                n = m.group(1).strip()
                if n and n not in seen:
                    seen.append(n)
        elif isinstance(v, list):
            for x in v:
                scan(x)
        elif isinstance(v, dict):
            for x in v.values():
                scan(x)
    for p in params:
        scan(p.get("value"))
    return [{"name": n} for n in seen]


# ── 변환 ─────────────────────────────────────────────────────────────────────


class _Stats:
    def __init__(self) -> None:
        self.disabled = 0
        self.comments = 0
        self.max_depth = 0


def _is_boilerplate_step(node: dict) -> bool:
    if (node.get("packageName"), node.get("commandName")) != ("Step", "step"):
        return False
    title = _step_title(node)
    return bool(title) and title.strip().lower() in BOILERPLATE_STEP_TITLES


def _convert_nodes(
    nodes: list, stats: _Stats, depth: int, in_boiler: bool, branch_of: str | None = None
) -> list[dict]:
    """노드 목록 → 액션 목록. branches는 형제로 승격해 이어 붙인다."""
    out: list[dict] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("disabled"):
            stats.disabled += 1
            continue
        pkg, cmd = node.get("packageName"), node.get("commandName")
        if not pkg or not cmd:
            continue
        if (pkg, cmd) in _DROP:
            stats.comments += 1
            continue
        boiler = in_boiler or _is_boilerplate_step(node)
        stats.max_depth = max(stats.max_depth, depth)

        params = _parameters(node)
        action: dict = {
            "order": len(out) + 1,
            "package": pkg,
            "action": cmd,
            "label": _step_title(node),
            "parameters": params,
            "children": _convert_nodes(node.get("children"), stats, depth + 1, boiler),
            "produces": _produces(node),
            "consumes": _consumes(params),
            "boilerplate": boiler,
            "container": container_kind(pkg, cmd),
            "uid": node.get("uid"),
        }
        if branch_of:
            # 이 액션이 어느 컨테이너의 분기인지 — 형제로 승격하면서 사라지는 정보라
            # 명시로 남긴다(구조 비교와 사람 감사 양쪽에 필요).
            action["branch_of"] = branch_of
        out.append(action)

        # branches(else/catch/finally)는 다음 형제로. `RecommendedAction`의 분기 표현 규약.
        for b in _convert_nodes(node.get("branches"), stats, depth, boiler, branch_of=cmd):
            b["order"] = len(out) + 1
            out.append(b)
    return out


def convert_bot_json(doc: dict, source_file: str) -> dict:
    """봇 JSON 하나 → 흐름도 스키마 dict (`Recommendation` 모양).

    최상위 `Step/step`은 단계로 올리고, 그 사이에 낀 최상위 비-Step 노드들은 **암묵 단계**로
    묶는다(실측 14파일 중 최상위 비-Step 노드가 34개 있다 — 버리면 정답이 줄어든다).
    """
    stats = _Stats()
    steps: list[dict] = []
    pending: list[dict] = []

    def flush() -> None:
        if not pending:
            return
        steps.append({
            "step_id": f"step-{len(steps) + 1}",
            "label": None,
            "source_file": source_file,
            "boilerplate": all(a.get("boilerplate") for a in pending),
            "implicit": True,
            "actions": [dict(a, order=i + 1) for i, a in enumerate(pending)],
        })
        pending.clear()

    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if node.get("disabled"):
            stats.disabled += 1
            continue
        pkg, cmd = node.get("packageName"), node.get("commandName")
        if (pkg, cmd) in _DROP:
            stats.comments += 1
            continue
        if (pkg, cmd) == ("Step", "step"):
            flush()
            boiler = _is_boilerplate_step(node)
            steps.append({
                "step_id": f"step-{len(steps) + 1}",
                "label": _step_title(node),
                "source_file": source_file,
                "boilerplate": boiler,
                "implicit": False,
                "actions": _convert_nodes(node.get("children"), stats, 2, boiler),
            })
            # 최상위 Step에 branches가 달리는 경우는 실측에 없지만, 있으면 버리지 않는다.
            for b in _convert_nodes(node.get("branches"), stats, 2, boiler, branch_of=cmd):
                steps[-1]["actions"].append(dict(b, order=len(steps[-1]["actions"]) + 1))
            continue
        converted = _convert_nodes([node], stats, 1, False)
        pending.extend(converted)
    flush()

    return {
        "source_file": source_file,
        "steps": steps,
        "variables": [
            {
                "name": v.get("name"),
                "type": (v.get("type") or "STRING"),
                "direction": "input" if v.get("input") else ("output" if v.get("output") else "local"),
            }
            for v in (doc.get("variables") or [])
            if isinstance(v, dict) and v.get("name")
        ],
        "stats": {
            "disabled": stats.disabled,
            "comments": stats.comments,
            "max_depth": stats.max_depth,
        },
    }


def convert_case(case_dir: Path) -> dict:
    """케이스 폴더(`workflows/*.json`) → 케이스 흐름도 하나.

    멀티봇 케이스(메인+서브태스크)는 파일명 순으로 단계를 이어 붙인다 —
    `gold.merged_sequence`와 같은 순서라 두 축이 같은 정답을 본다.
    """
    steps: list[dict] = []
    variables: list[dict] = []
    files: list[str] = []
    stats = {"disabled": 0, "comments": 0, "max_depth": 0}
    for path in sorted((case_dir / "workflows").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        flow = convert_bot_json(doc, path.name)
        files.append(path.name)
        for st in flow["steps"]:
            steps.append(dict(st, step_id=f"step-{len(steps) + 1}"))
        variables.extend(flow["variables"])
        stats["disabled"] += flow["stats"]["disabled"]
        stats["comments"] += flow["stats"]["comments"]
        stats["max_depth"] = max(stats["max_depth"], flow["stats"]["max_depth"])
    return {
        "schema_version": "gold-flow/1.0",
        "source_files": files,
        "steps": steps,
        "variables": variables,
        "stats": stats,
    }


# ── 순회 ─────────────────────────────────────────────────────────────────────


def iter_flow_actions(flow: dict, *, include_scaffold: bool = False):
    """(액션, 중첩경로) 쌍을 pre-order로 순회한다. 정답지·에이전트 출력 **양쪽**에 쓴다.

    중첩경로에서 `step`은 뺀다. 정답 봇은 Step 노드로 구획하고 에이전트는 `steps[]`와
    `Step/Step` 액션을 섞어 쓰므로(실측: 한 런에 `Step/Step` 액션 257개), Step을 경로에
    넣으면 같은 구조도 다른 경로로 읽힌다. 우리가 재려는 건 "이 액션이 Loop 안의 If 안에
    있는가"지 "몇 번째 구획에 있는가"가 아니다.
    """
    def walk(actions, path):
        for a in actions or []:
            # 에이전트 산출물도 이 함수로 순회한다 — 외부 JSON이므로 모양을 믿지 않는다.
            # 한 노드가 이상하다고 채점 전체가 AttributeError로 죽으면 안 된다.
            if not isinstance(a, dict):
                continue
            pkg, act = a.get("package"), a.get("action")
            if not pkg or not act:
                continue
            scaffold = is_scaffold_action(pkg, act)
            if include_scaffold or not scaffold:
                yield a, path
            kind = a.get("container")
            if kind is None:
                kind = container_kind(pkg, act)
            child_path = path if (kind is None or kind == "step") else path + (kind,)
            yield from walk(a.get("children"), child_path)

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            yield from walk(step.get("actions"), ())


# ── KB 어휘 사상 ─────────────────────────────────────────────────────────────

# 봇 Loop 노드는 액션 이름이 늘 `loop.commands.start`고, **반복자(ITERATOR 속성)**가 무슨
# 루프인지를 정한다. 반면 KB는 반복자별로 액션이 따로 있다(`For each row in table` 등).
# 그래서 Loop만은 액션 이름이 아니라 반복자로 사상해야 한다 — 이름으로 풀면 토큰이 하나도
# 안 겹쳐(`{loop}` vs `{for,data,iteration}`) 전건 unresolved가 된다.
_ITERATOR_TO_KB = {
    "Loop/loop.iterators.times": ("Loop", "Loop action for data iteration"),
    "DataTable/iteratorTableRow": ("Loop", "For each row in table"),
    "Email/loop.iterators.email": ("Loop", "For each mail in mail box"),
    # 현 KB에 '엑셀 워크시트의 각 행' 반복자 문서가 없다(2026-07-26 카탈로그 1,200건 확인).
    # 컨트롤룸에서는 같은 Loop 액션의 반복자 설정일 뿐이라 제네릭 Loop로 사상하고, 실제
    # 반복자는 파라미터에 그대로 남아 감사 가능하다.
    "Excel_MS/loop.iterators.excel": ("Loop", "Loop action for data iteration"),
    "Excel/loop.iterators.excel": ("Loop", "Loop action for data iteration"),
}

# 반복자 속성이 아예 없는 Loop(조건 루프 등)의 기본 사상
_LOOP_DEFAULT_KB = ("Loop", "Loop action for data iteration")


class KbResolver:
    """봇 표기 (package, action) → KB 카탈로그 표기. **빌드 시 1회** 풀고 결과를 박아 둔다.

    근거는 `notation.py`의 기존 별칭·토큰 유사도 그대로다(같은 어휘 판단을 두 벌 유지하지
    않으려고). 다른 점은 **언제 쓰느냐**다: 채점마다가 아니라 정답지 생성 때 한 번만 쓰고,
    결과·유사도·동률 후보를 파일에 남긴다. 그래서 채점기는 문자열 일치만 하면 된다.

    동률 처리: 최고 유사도가 같은 후보가 여럿이면 (package, action) 사전순으로 고른다 —
    DB 반환 순서에 사상이 흔들리면 정답지가 빌드마다 달라진다. 버려진 동률 후보는
    `alternatives`에 남겨 사람이 교정 표(overrides)로 뒤집을 수 있게 한다.
    """

    def __init__(self, kb_specs: list[dict], overrides: dict | None = None) -> None:
        self._by_pkg: dict[str, list[tuple[str, str, CanonAction]]] = {}
        for spec in kb_specs:
            pkg, act = spec.get("package"), spec.get("action")
            if not pkg or not act:
                continue
            ca = CanonAction(pkg, act)
            self._by_pkg.setdefault(ca.pkg_key, []).append((pkg, act, ca))
        for lst in self._by_pkg.values():
            lst.sort(key=lambda x: (x[0], x[1]))
        self._overrides = overrides or {}
        self.n_kb = sum(len(v) for v in self._by_pkg.values())

    def resolve(self, package: str, action: str, parameters: list[dict] | None = None) -> dict:
        """한 액션의 KB 사상 결과. 항상 dict를 돌려주며 실패도 `status="unresolved"`로 남긴다."""
        key = f"{package}/{action}"
        if key in self._overrides:
            tgt, why = self._overrides[key]
            if tgt is None:
                return {"status": "unresolved", "via": "override", "package": None,
                        "action": None, "sim": None,
                        "note": why or "수동 교정: 사상 없음"}
            return {"status": "resolved", "via": "override", "package": tgt[0],
                    "action": tgt[1], "sim": 1.0, "note": why or "수동 교정"}

        pkg_key = canon_package(package)

        # Loop는 반복자가 정체성이다 (위 _ITERATOR_TO_KB 주석 참조)
        if pkg_key == "loop" and container_kind(package, action) == "loop":
            it = _iterator_of(parameters)
            if it and it in _ITERATOR_TO_KB:
                p, a = _ITERATOR_TO_KB[it]
                return {"status": "resolved", "via": "iterator", "package": p, "action": a,
                        "sim": 1.0, "discriminator": it, "note": f"반복자 {it}"}
            p, a = _LOOP_DEFAULT_KB
            return {"status": "resolved", "via": "iterator", "package": p, "action": a,
                    "sim": 1.0, "discriminator": it or "none",
                    "note": f"반복자 {it or '없음'} — KB에 전용 반복자 액션이 없어 제네릭 Loop로"}

        gold = CanonAction(package, action)
        best = 0.0
        winners: list[tuple[str, str]] = []
        for pkg, act, ca in self._by_pkg.get(pkg_key, []):
            s = gold.sim(ca)
            if s > best:
                best, winners = s, [(pkg, act)]
            elif s == best and s > 0:
                winners.append((pkg, act))
        if best >= MATCH_THRESHOLD and winners:
            chosen = winners[0]  # 위에서 사전순 정렬해 두었으므로 결정론적
            out = {"status": "resolved", "via": "fuzzy", "package": chosen[0],
                   "action": chosen[1], "sim": round(best, 3)}
            if len(winners) > 1:
                out["alternatives"] = [list(w) for w in winners[1:6]]
            return out

        # KB에 스키마가 없어도 검수기가 허용하는 컨테이너(If/Error handler/Step)는
        # 에이전트가 낼 수 있으므로 정답지에서도 '표기 확정'으로 취급한다.
        kind = container_kind(package, action)
        if kind and pkg_key in CONTAINER_PKG_KEYS:
            return {"status": "resolved", "via": "container", "package": package,
                    "action": kind, "sim": None,
                    "note": "KB 스키마 없음 — 정준 컨테이너 표기로 고정"}

        return {"status": "unresolved", "via": "fuzzy", "package": None, "action": None,
                "sim": round(best, 3) if best else 0.0,
                "note": f"KB 최고 유사도 {best:.2f} < {MATCH_THRESHOLD}"}


def _iterator_of(parameters: list[dict] | None) -> str | None:
    for p in parameters or []:
        if p.get("type") == "ITERATOR" and isinstance(p.get("value"), str):
            return p["value"]
    return None


def annotate_kb(flow: dict, resolver: KbResolver) -> dict:
    """흐름도의 모든 액션에 `kb` 사상을 박는다. 원본 dict를 제자리에서 고친다."""
    def walk(actions):
        for a in actions or []:
            if a.get("package") and a.get("action"):
                a["kb"] = resolver.resolve(a["package"], a["action"], a.get("parameters"))
            walk(a.get("children"))

    for step in flow.get("steps") or []:
        walk(step.get("actions"))
    return flow


def load_overrides(path: Path) -> dict:
    """수동 교정 표. 값은 세 형태를 받는다:

        "Pkg/action": ["KB Pkg", "KB action"]                 # 그 표기로 고정
        "Pkg/action": {"kb": ["KB Pkg", "KB action"], "why": …}  # 근거를 같이 적는 형태
        "Pkg/action": null                                     # '사상 없음'을 강제

    왜 파일인가: 자동 사상은 동률·표기 사고를 완벽히 못 푼다. 코드를 고치지 않고 정답지만
    고칠 수 있어야 어휘 판단의 근거가 diff로 남는다. `why`를 dict로 받는 이유는 JSON에
    주석이 없어서다 — 근거 없는 교정은 나중에 아무도 검증하지 못한다.
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict = {}
    for k, v in raw.items():
        if k.startswith("_"):  # "_comment" 같은 설명 키
            continue
        if isinstance(v, dict):
            kb = v.get("kb")
            out[k] = (tuple(kb), v.get("why")) if isinstance(kb, list) else (None, v.get("why"))
        elif isinstance(v, list):
            out[k] = (tuple(v), None)
        else:
            out[k] = (None, None)
    return out
