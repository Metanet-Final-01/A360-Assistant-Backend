# -*- coding: utf-8 -*-
"""고정 정답지 대비 **엄밀 채점** — 퍼지 유사도 없음 (RPA-298 Phase 0-1).

`metrics.score_case`(기존 축)와 무엇이 다른가:

| | 기존 `action`/`action_core`/`action_equiv` | 여기 `action_exact` |
|---|---|---|
| 매칭 | 토큰 Jaccard ≥ 0.55 탐욕 1:1 | KB 표기 **문자열 일치** |
| 중첩 | 안 봄 | 중첩 경로 일치율 |
| 파라미터 | 안 봄 | (별도 축, 이름 집합 겹침) |
| 정답 표기 | 매 채점마다 퍼지로 다시 풂 | 빌드 때 **한 번 풀어 박은** 정답지 |

⚠️ 기존 축을 대체하지 않는다. 지난 기준선 런이 전부 기존 축으로 재졌으므로 둘이 나란히
산출돼야 비교가 유지된다. 이 축은 "정답 봇과 **같은 액션을 같은 자리에** 놓았는가"라는
더 좁고 엄한 질문을 잰다 — 수치는 당연히 낮게 나오며, 낮은 게 정상이다.

정규화는 대소문자·구분자 접기만 한다. 그건 표기 사고를 지우는 것이지 의미를 추정하는 게
아니다. 의미 추정(어느 KB 액션이 이 봇 액션이냐)은 전부 정답지 빌드 때 끝나 있다.
"""

import re
from collections import Counter
from dataclasses import dataclass

from .flow_schema import container_kind, iter_flow_actions
from .metrics import _lis_len
from .notation import canon_package


def exact_key(package: str, action: str) -> tuple[str, str]:
    """비교용 정준 키 — 패키지 정준 키 + 구분자·대소문자를 접은 액션명.

    `Excel advanced`/`Excel_MS`는 `canon_package`가 같은 키로 접는다(어휘 별칭은 정답지
    빌드 때 이미 확정됐고, 여기서는 **패키지 표기 흔들림**만 흡수한다).
    액션명은 `For each row in CSV/TXT` → `foreachrowincsvtxt`처럼 영숫자만 남긴다.
    """
    return (canon_package(package), re.sub(r"[^0-9a-z]+", "", (action or "").lower()))


@dataclass
class Slot:
    """채점 대상 액션 하나 — 위치·정준 키·중첩 경로."""

    index: int
    key: tuple[str, str] | None  # None이면 사상 실패(정답 쪽) — 절대 매칭되지 않는다
    raw: tuple[str, str]
    path: tuple[str, ...]
    kind: str | None
    boilerplate: bool = False
    resolved: bool = True


def gold_slots(flow: dict) -> list[Slot]:
    """고정 정답지(`flow_schema.convert_case` + `annotate_kb` 산출) → 채점 슬롯.

    `kb.status != "resolved"`인 액션은 키를 `None`으로 둔다 — 버리지 않고 남겨서
    분모(전체 재현율)에는 들어가되 절대 맞지 않게 한다. 몇 건이 그런지가 이 평가의
    1급 정보(KB 결손 규모)라 조용히 빼면 안 된다.
    """
    out: list[Slot] = []
    for i, (a, path) in enumerate(iter_flow_actions(flow)):
        kb = a.get("kb") or {}
        resolved = kb.get("status") == "resolved" and kb.get("package") and kb.get("action")
        out.append(Slot(
            index=i,
            key=exact_key(kb["package"], kb["action"]) if resolved else None,
            raw=(a["package"], a["action"]),
            path=path,
            kind=a.get("container") if a.get("container") is not None
            else container_kind(a["package"], a["action"]),
            boilerplate=bool(a.get("boilerplate")),
            resolved=bool(resolved),
        ))
    return out


def pred_slots(recommendation: dict) -> list[Slot]:
    """에이전트 `recommendation.json` → 채점 슬롯. 이미 KB 표기라 사상 없이 그대로 쓴다."""
    out: list[Slot] = []
    for i, (a, path) in enumerate(iter_flow_actions(recommendation)):
        pkg, act = a.get("package"), a.get("action")
        out.append(Slot(
            index=i,
            key=exact_key(pkg, act),
            raw=(pkg, act),
            path=path,
            kind=container_kind(pkg, act),
        ))
    return out


def _prf(n_match: int, n_pred: int, n_gold: int) -> dict:
    p = n_match / n_pred if n_pred else 0.0
    r = n_match / n_gold if n_gold else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}


def pair_slots(gold: list[Slot], pred: list[Slot]) -> list[tuple[Slot, Slot]]:
    """정준 키가 같은 것끼리 **등장 순서대로** 1:1로 잇는다.

    같은 키가 양쪽에 여러 번 나오면(`String/Assign`이 흔하다) i번째끼리 짝짓는다. 탐욕
    유사도 정렬 대신 이걸 쓰는 이유: (1) 결정론이다 — 동률 흔들림이 없다. (2) 같은 키끼리는
    순서대로 잇는 게 교차를 최소화해 순서 지표가 표기 우연이 아니라 실제 배치 차이를 잰다.
    """
    buckets: dict[tuple[str, str], list[Slot]] = {}
    for s in pred:
        buckets.setdefault(s.key, []).append(s)
    pairs: list[tuple[Slot, Slot]] = []
    for g in gold:
        if g.key is None:
            continue
        bucket = buckets.get(g.key)
        if bucket:
            pairs.append((g, bucket.pop(0)))
    return pairs


def nesting_report(pairs: list[tuple[Slot, Slot]], gold: list[Slot], pred: list[Slot]) -> dict:
    """중첩 구조 일치 — 매칭된 액션이 **같은 컨테이너 안에** 있는가.

    `path_match`가 이 축의 핵심이다. 액션 이름을 다 맞혀도 Loop 밖에 놓으면 봇이 다르게
    돈다 — 기존 퍼지 축은 그걸 아예 못 봤다(평평한 시퀀스만 비교했으므로).
    `depth_match`는 완화판(깊이만 같으면 인정)으로, 둘의 차이가 "컨테이너 종류를 틀렸나
    자리만 틀렸나"를 가른다.
    """
    n = len(pairs)
    exact = sum(1 for g, p in pairs if g.path == p.path)
    depth = sum(1 for g, p in pairs if len(g.path) == len(p.path))
    cg = Counter(s.kind for s in gold if s.kind)
    cp = Counter(s.kind for s in pred if s.kind)
    overlap = sum((cg & cp).values())
    return {
        "n_compared": n,
        "path_match": round(exact / n, 3) if n else None,
        "depth_match": round(depth / n, 3) if n else None,
        "container": {
            **_prf(overlap, sum(cp.values()), sum(cg.values())),
            "gold": dict(sorted(cg.items())),
            "pred": dict(sorted(cp.items())),
        },
        "max_depth_gold": max((len(s.path) for s in gold), default=0),
        "max_depth_pred": max((len(s.path) for s in pred), default=0),
        "mismatched": [
            {"action": list(g.raw), "gold_path": list(g.path), "pred_path": list(p.path)}
            for g, p in pairs if g.path != p.path
        ][:40],
    }


def order_exact(pairs: list[tuple[Slot, Slot]]) -> float | None:
    """정답 순서로 늘어놓은 예측 위치의 LIS 비율 (매칭쌍 2개 미만이면 None).

    기존 축과 같은 정의를 쓴다 — 순서 지표만은 두 축을 직접 비교할 수 있어야
    "엄밀 매칭으로 바꿨더니 순서가 나빠 보인다"가 매칭 모집단 차이인지 실제인지 갈린다.
    """
    if len(pairs) < 2:
        return None
    by_gold = sorted(pairs, key=lambda gp: gp[0].index)
    return round(_lis_len([p.index for _g, p in by_gold]) / len(by_gold), 3)


def param_report(pairs: list[tuple[Slot, Slot]], gold_flow: dict, recommendation: dict) -> dict:
    """매칭된 액션의 파라미터 **이름 집합** 겹침.

    값은 비교하지 않는다: 정답 봇의 값은 그 회사의 실제 경로·계정이라 문서에서 유도할 수
    없다(`file:///C:/Users/…/Configuration.xml`). 반면 "어떤 파라미터를 채웠는가"는 문서에서
    도출 가능한 판단이라 잴 값어치가 있다.
    """
    gold_params = [set(_param_names(a)) for a, _ in iter_flow_actions(gold_flow)]
    pred_params = [set(_param_names(a)) for a, _ in iter_flow_actions(recommendation)]
    n = inter = union = 0
    for g, p in pairs:
        gs = gold_params[g.index] if g.index < len(gold_params) else set()
        ps = pred_params[p.index] if p.index < len(pred_params) else set()
        if not gs and not ps:
            continue
        n += 1
        inter += len(gs & ps)
        union += len(gs | ps)
    return {
        "n_compared": n,
        "name_jaccard": round(inter / union, 3) if union else None,
    }


def _param_names(action: dict) -> list[str]:
    out = []
    for p in action.get("parameters") or []:
        if isinstance(p, dict) and p.get("name"):
            # 대소문자·공백만 접는다 — `Session name` ↔ `sessionName`
            out.append(re.sub(r"[^0-9a-z]+", "", str(p["name"]).lower()))
    return out


def _scalar(v):
    """요약 행에 실을 수 있는 값만 통과시킨다(수치 또는 '못 쟀음'을 뜻하는 None).

    중첩·순서 지표는 매칭쌍이 없으면 None을 낸다 — 그 None은 0으로 접으면 안 된다.
    '측정 못 함'과 '0점'은 다른 사실이고, 평균에 0이 섞이면 리포트가 거짓말을 한다.
    """
    return v if isinstance(v, (int, float)) or v is None else None


# 엄밀 채점이 기존 score dict에 **얹는** 키. `rescore`·`run_eval`이 이 목록 하나만 보게
# 해서, 축을 늘릴 때 두 경로가 갈리지 않게 한다(갈리면 재채점 수치를 원 런과 못 비교한다).
EXACT_SCORE_KEYS = (
    "action_exact",
    "action_exact_resolved",
    "action_exact_core",
    "nesting",
    "order_exact",
    "unresolved_gold",
)


def attach_exact_axes(score: dict, recommendation: dict, gold_flow: dict) -> dict:
    """기존 축 dict에 엄밀 축을 얹는다 — **런너와 재채점기의 공용 진입점**.

    두 경로가 각자 `score_case_exact`를 호출해 각자 키를 골라 담으면, 한쪽만 축을 늘렸을 때
    재채점 수치를 원 런과 비교할 수 없게 된다. 그래서 '무슨 키를 어떤 이름으로 얹는가'를
    여기 한 곳에만 둔다. 기존 축(`action`/`action_core`/`action_equiv`) 값은 건드리지 않는다.
    """
    ex = score_case_exact(recommendation, gold_flow)
    for k in EXACT_SCORE_KEYS:
        score[k] = ex[k]
    score["exact_detail"] = {
        "n_pred": ex["n_pred"], "n_gold": ex["n_gold"], "n_matched": ex["n_matched"],
        "parameters": ex["parameters"],
    }
    return score


# 요약 행(run_eval의 summary/report)에 실을 엄밀 축 스칼라. 기존 행 키와 충돌하지 않게
# 전부 `_exact`/`nesting_` 접두·접미를 쓴다 — 기존 키는 한 글자도 바뀌면 안 된다.
EXACT_ROW_KEYS = (
    "f1_exact", "precision_exact", "recall_exact",
    "f1_exact_core", "recall_exact_core", "recall_exact_resolved",
    "nesting_path", "nesting_depth", "order_exact",
    "n_gold_unresolved", "param_jaccard",
)


def exact_row(score: dict) -> dict:
    """`attach_exact_axes`가 얹은 결과 → 요약 행 스칼라. 엄밀 축이 없으면 빈 dict.

    엄밀 축이 꺼진 런에서는 **키 자체가 안 생겨야** 한다 — 0.0으로 채우면 리포트가
    '엄밀 f1 0점'이라는 거짓 신호를 낸다(측정을 안 한 것과 0점은 다르다).
    """
    if "action_exact" not in score:
        return {}
    ae, aer, aec = score["action_exact"], score["action_exact_resolved"], score["action_exact_core"]
    nest = score.get("nesting") or {}
    return {
        "f1_exact": ae["f1"],
        "precision_exact": ae["precision"],
        "recall_exact": ae["recall"],
        "f1_exact_core": aec["f1"],
        "recall_exact_core": aec["recall"],
        "recall_exact_resolved": aer["recall"],
        "nesting_path": _scalar(nest.get("path_match")),
        "nesting_depth": _scalar(nest.get("depth_match")),
        "order_exact": _scalar(score.get("order_exact")),
        "n_gold_unresolved": aer["n_gold_unresolved"],
        "param_jaccard": _scalar((score.get("exact_detail") or {}).get("parameters", {}).get("name_jaccard")),
    }


def score_case_exact(recommendation: dict, gold_flow: dict) -> dict:
    """고정 정답지 대비 엄밀 채점 결과 한 벌.

    `recommendation`은 에이전트 산출 dict(`recommendation.json`),
    `gold_flow`는 `build_gold_flows`가 만든 케이스 정답지 dict.
    """
    gold = gold_slots(gold_flow)
    pred = pred_slots(recommendation)
    pairs = pair_slots(gold, pred)

    n_gold, n_pred, n_match = len(gold), len(pred), len(pairs)
    n_gold_resolved = sum(1 for s in gold if s.resolved)
    n_gold_core = sum(1 for s in gold if not s.boilerplate)
    n_match_core = sum(1 for g, _p in pairs if not g.boilerplate)
    n_match_resolved = sum(1 for g, _p in pairs if g.resolved)  # 정의상 n_match와 같다

    matched_gold = {g.index for g, _ in pairs}
    matched_pred = {p.index for _, p in pairs}

    unresolved = Counter(f"{s.raw[0]}/{s.raw[1]}" for s in gold if not s.resolved)

    return {
        "n_pred": n_pred,
        "n_gold": n_gold,
        "n_matched": n_match,
        # 전체 정답 대비 — 사상 실패분이 분모에 남아 재현율 천장이 낮다(의도된 정직함)
        "action_exact": _prf(n_match, n_pred, n_gold),
        # KB 어휘로 사상된 정답만 — 에이전트가 **표기상 낼 수 있었던** 것 대비
        "action_exact_resolved": {
            **_prf(n_match_resolved, n_pred, n_gold_resolved),
            "n_gold_resolved": n_gold_resolved,
            "n_gold_unresolved": n_gold - n_gold_resolved,
        },
        # Bot Store 제출 규약 보일러플레이트를 뺀 실업무만 (기존 action_core와 같은 취지)
        "action_exact_core": {
            **_prf(n_match_core, n_pred, n_gold_core),
            "n_gold_core": n_gold_core,
        },
        "nesting": nesting_report(pairs, gold, pred),
        "order_exact": order_exact(pairs),
        "parameters": param_report(pairs, gold_flow, recommendation),
        "unresolved_gold": [{"action": k, "count": v} for k, v in unresolved.most_common()],
        "pairs": [
            {"gold": list(g.raw), "pred": list(p.raw),
             "gold_path": list(g.path), "pred_path": list(p.path)}
            for g, p in pairs
        ][:200],
        "gold_only": [
            {"action": list(s.raw), "path": list(s.path), "resolved": s.resolved}
            for s in gold if s.index not in matched_gold
        ][:200],
        "pred_only": [
            {"action": list(s.raw), "path": list(s.path)}
            for s in pred if s.index not in matched_pred
        ][:200],
    }
