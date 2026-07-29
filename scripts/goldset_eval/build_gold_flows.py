# -*- coding: utf-8 -*-
"""정답 봇 JSON → **고정된 정답지**(흐름도 스키마 + KB 어휘) 생성기 (RPA-298 Phase 0-1).

산출물은 `final-etc-files/골드셋/정답흐름도/` 아래에 커밋된다. 이 스크립트를 같이 두는
이유는 재생성 가능해야 하기 때문이다 — 카탈로그가 바뀌면 사상률이 바뀌고, 그 변화가
diff로 보여야 "KB를 채웠더니 정답지가 얼마나 더 풀렸나"를 잴 수 있다.

사용:
    PYTHONUTF8=1 \\
    RAG_DATABASE_URL="postgresql://a360_admin:a360_local_password@localhost:5433/a360" \\
    OPENSEARCH_HOST="http://localhost:9201" OPENSEARCH_USERNAME="" OPENSEARCH_PASSWORD="" \\
    .venv/Scripts/python.exe -m scripts.goldset_eval.build_gold_flows \\
      --goldset "C:\\...\\final-etc-files\\골드셋" [--out <dir>] [--no-catalog]

⚠️ `RAG_DATABASE_URL`을 덮어쓰지 않으면 `.env`의 **운영 Neon**에서 카탈로그를 읽는다.
   (`run_eval` README와 같은 주의 — 여기도 카탈로그를 전량 읽는다.)

`--no-catalog`는 KB 사상 없이 변환만 한다(DB 없는 환경에서 스키마 변환만 확인할 때).
그 산출물은 정답지로 쓰면 안 된다 — 전건이 unresolved가 된다. 그래서 **정본 자리에는
쓰지 못하게 코드가 막는다**(`--out`으로 다른 곳을 지정하거나 `--force`가 필요하다):
산출물이 있는 `final-etc-files`는 git 저장소가 아니라 덮어쓰면 복구 수단이 없고,
`rescore`/`run_eval`은 정답지가 "있으면" 엄밀 축을 켜므로 망가진 정답지 위에서 0에
가까운 그럴듯한 수치가 나온다.
"""

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .flow_schema import KbResolver, annotate_kb, convert_case, iter_flow_actions, load_overrides
from .gold import load_case, merged_sequence

# 수동 교정 표의 기본 위치 — 코드 옆에 둔다(어휘 판단은 하네스의 일부지 데이터가 아니다).
OVERRIDES_PATH = Path(__file__).with_name("gold_flow_overrides.json")

# 산출 디렉터리 기본 이름 (골드셋 루트 기준)
DEFAULT_OUT_NAME = "정답흐름도"

# 빌더 세대. 변환 규약을 **의도적으로** 바꿨을 때 사람이 올린다(해시는 우연한 변경까지
# 잡지만 "무엇이 왜 바뀌었나"는 못 담는다).
BUILDER_VERSION = "gold-flow-builder/1.0"

# 정본 자리에 쓰려면 넘겨야 하는 최소 KB 사상률. 실측 정본은 0.96이고, 카탈로그가 비거나
# 엉뚱한 DB를 봤을 때 0.1 언저리로 떨어진다 — 그 사이 어디를 잘라도 되지만 사고를 잡는
# 게 목적이라 넉넉히 절반으로 둔다.
MIN_RESOLVE_RATE = 0.5

# 변환 규약이 사는 곳. 산출물은 리포 밖(final-etc-files)이라 코드가 바뀌어도 산출물은
# 조용히 낡는다 — 이 파일들의 지문을 manifest에 박아 소비처가 불일치를 감지한다.
# `build_gold_flows.py` 자신은 넣지 않는다: 여기 바뀌는 건 대개 CLI·가드지 변환 규약이
# 아니라, 넣으면 산출물과 무관한 편집마다 "낡았다"고 외치는 오탐이 된다.
_CONVENTION_SOURCES = (Path(__file__).with_name("flow_schema.py"), OVERRIDES_PATH)


class GoldFlowBuildRefused(RuntimeError):
    """정본을 덮어쓸 뻔한 빌드를 막았다. 메시지에 해제 방법이 들어 있다."""


def _py_digest(path: Path) -> str:
    """파이썬 소스의 **구조** 지문 — 주석·docstring·포매팅 변화는 무시한다.

    주석 밀도 규약상 설명이 자주 손질되는데 그때마다 "산출물이 낡았다"고 경고하면
    아무도 그 경고를 안 읽게 된다. AST로 접어 의미 있는 변경만 잡는다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body = body[1:]
    return hashlib.sha256(ast.dump(tree).encode("utf-8")).hexdigest()


def convention_hash() -> str:
    """변환 규약(스키마 변환기 + 수동 교정 표)의 지문."""
    h = hashlib.sha256()
    for p in _CONVENTION_SOURCES:
        if not p.is_file():
            h.update(b"<missing>")
        elif p.suffix == ".py":
            h.update(_py_digest(p).encode())
        else:
            h.update(json.dumps(json.loads(p.read_text(encoding="utf-8")),
                                sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


def _kb_specs() -> list[dict]:
    from app.services.catalog import get_backend_catalog

    return list(get_backend_catalog().iter_action_schemas())


def read_manifest(flows_dir: Path) -> dict | None:
    """정답지 manifest를 읽는다. 없거나 깨졌으면 None(소비처가 판단하게)."""
    p = flows_dir / "manifest.json"
    if not p.is_file():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _protected_reason(out_dir: Path) -> str | None:
    """이 경로가 '덮어쓰면 복구 못 하는 정본 자리'인가 — 아니면 None."""
    if out_dir.name == DEFAULT_OUT_NAME:
        return f"기본 산출 경로({DEFAULT_OUT_NAME}/)"
    prev = read_manifest(out_dir)
    if prev and (prev.get("catalog") or {}).get("used"):
        return f"카탈로그로 만든 정답지가 이미 있음(생성 {prev.get('generated_at')})"
    return None


def _refusal(out_dir: Path, *, use_catalog: bool, resolve_rate: float | None) -> str | None:
    """정본 자리에 **망가진 정답지**를 쓰려는 빌드면 거부 사유, 아니면 None.

    거부는 docstring 경고로 대신할 수 없다: 산출물이 사는 `final-etc-files`는 git 저장소가
    아니라(실측: `git rev-parse` fatal) 덮어쓰면 되돌릴 수 없고, `manifest.json`은 정상적으로
    생기므로 소비처는 그게 망가진 정답지인지 알 수 없다.
    """
    where = _protected_reason(out_dir)
    if not where:
        return None
    why: list[str] = []
    if not use_catalog:
        why.append("KB 카탈로그 없이 빌드 — 전건 unresolved가 된다")
    if resolve_rate is not None and resolve_rate < MIN_RESOLVE_RATE:
        why.append(f"KB 사상률 {resolve_rate:.1%} < {MIN_RESOLVE_RATE:.0%} — 카탈로그가 비었거나 "
                   f"엉뚱한 DB를 본 것으로 보인다")
    if not why:
        return None
    return (f"정본을 덮어쓸 뻔했다 — 쓰지 않았다.\n  대상: {out_dir} ({where})\n  "
            + "\n  ".join(f"사유: {w}" for w in why)
            + "\n  이 산출물이 사는 트리는 git 저장소가 아니라 덮어쓰면 복구할 수 없다.\n"
              "  정말 필요하면 --out 으로 다른 경로를 주거나(권장) --force 를 붙여라.")


def build(goldset: Path, out_dir: Path, *, use_catalog: bool = True, force: bool = False) -> dict:
    """13케이스 전부 변환·사상하고 산출물을 쓴다. 반환은 manifest dict.

    산출물은 **전부 메모리에 만든 뒤 한 번에** 쓴다. 케이스마다 바로 쓰면 사상률 같은
    전체 지표로 거부 판단을 내리기 전에 정본이 이미 절반 덮여 있게 된다.
    """
    manifest_in = json.loads(
        (goldset / "정답셋" / "manifest.json").read_text(encoding="utf-8")
    )
    overrides = load_overrides(OVERRIDES_PATH)
    specs = _kb_specs() if use_catalog else []
    resolver = KbResolver(specs, overrides)

    pending: list[tuple[Path, str]] = []  # (경로, 내용) — 가드를 통과한 뒤에만 디스크로

    entries: list[dict] = []
    vocab: dict[str, dict] = {}
    tot_actions = tot_resolved = 0

    for e in manifest_in["entries"]:
        case_dir = goldset / "정답셋" / e["case_dir"]
        flow = convert_case(case_dir)
        annotate_kb(flow, resolver)

        actions = [a for a, _p in iter_flow_actions(flow)]
        n_res = sum(1 for a in actions if (a.get("kb") or {}).get("status") == "resolved")

        # 무결성 검사: 변환 트리를 평탄화한 액션 시퀀스가 기존 `gold.merged_sequence`와
        # 같아야 한다. 다르면 두 채점 축이 서로 다른 정답을 보게 되므로 비교가 무의미해진다.
        legacy = merged_sequence(load_case(case_dir))
        mine = [(a["package"], a["action"]) for a in actions]
        drift = None if mine == legacy else {"legacy": len(legacy), "converted": len(mine)}

        flow.update({
            "index": e["index"],
            "case_dir": e["case_dir"],
            "bot_name": e["bot_name"],
            "title": e.get("title", ""),
        })
        pending.append((out_dir / "cases" / f"{e['case_dir']}.json",
                        json.dumps(flow, ensure_ascii=False, indent=2)))

        for a in actions:
            kb = a.get("kb") or {}
            # 사상 대상까지 키에 넣는다. 봇 표기 하나가 **여러 KB 액션으로 갈릴 수 있기**
            # 때문이다 — Loop는 늘 `loop.commands.start`지만 반복자에 따라 실제 KB 액션이
            # 다르다(실측 4종). 봇 표기만으로 묶으면 13건이 한 줄로 뭉개져 표가 거짓말을 한다.
            key = (f"{a['package']}/{a['action']}\t{kb.get('package')}/{kb.get('action')}"
                   f"\t{kb.get('discriminator') or ''}")
            row = vocab.setdefault(key, {
                "gold": [a["package"], a["action"]],
                "kb": [kb.get("package"), kb.get("action")] if kb.get("status") == "resolved" else None,
                "status": kb.get("status", "unresolved"),
                "via": kb.get("via"),
                "sim": kb.get("sim"),
                "discriminator": kb.get("discriminator"),
                "note": kb.get("note"),
                "alternatives": kb.get("alternatives"),
                "count": 0,
                "cases": [],
            })
            row["count"] += 1
            if e["index"] not in row["cases"]:
                row["cases"].append(e["index"])

        tot_actions += len(actions)
        tot_resolved += n_res
        entries.append({
            "index": e["index"],
            "case_dir": e["case_dir"],
            "bot_name": e["bot_name"],
            "file": f"cases/{e['case_dir']}.json",
            "n_steps": len(flow["steps"]),
            "n_actions": len(actions),
            "n_resolved": n_res,
            "resolve_rate": round(n_res / len(actions), 3) if actions else None,
            "max_depth": flow["stats"]["max_depth"],
            "comments_dropped": flow["stats"]["comments"],
            "disabled_dropped": flow["stats"]["disabled"],
            "sequence_matches_legacy_gold": drift is None,
            **({"drift": drift} if drift else {}),
        })

    unresolved = sorted(
        (v for v in vocab.values() if v["status"] != "resolved"),
        key=lambda v: -v["count"],
    )
    manifest = {
        "schema_version": "gold-flow/1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "scripts.goldset_eval.build_gold_flows",
        # 산출물은 리포 밖에 있어 코드가 바뀌어도 조용히 낡는다 — 소비처가 대조할 지문.
        "builder": {"version": BUILDER_VERSION, "convention_hash": convention_hash()},
        "source_manifest": str(goldset / "정답셋" / "manifest.json"),
        "catalog": {"used": use_catalog, "n_action_schemas": len(specs)},
        "overrides": {"path": OVERRIDES_PATH.name, "n": len(overrides)},
        "totals": {
            "cases": len(entries),
            "actions": tot_actions,
            "resolved": tot_resolved,
            "unresolved": tot_actions - tot_resolved,
            "resolve_rate": round(tot_resolved / tot_actions, 4) if tot_actions else None,
            # 봇 표기 종수와 사상 행 수는 다르다 — Loop처럼 한 표기가 여러 KB 액션으로 갈린다.
            "distinct_gold_notations": len({tuple(v["gold"]) for v in vocab.values()}),
            "distinct_mappings": len(vocab),
            "distinct_unresolved": len(unresolved),
        },
        "entries": entries,
    }
    pending += [
        (out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)),
        (out_dir / "vocabulary.json", json.dumps(
            {"generated_at": manifest["generated_at"],
             "mappings": [vocab[k] for k in sorted(vocab)]},
            ensure_ascii=False, indent=2)),
        (out_dir / "unresolved.json", json.dumps(
            {"generated_at": manifest["generated_at"],
             "n_distinct": len(unresolved),
             "n_occurrences": sum(v["count"] for v in unresolved),
             "items": unresolved}, ensure_ascii=False, indent=2)),
        (out_dir / "README.md", _readme(manifest, unresolved)),
    ]

    # 여기가 유일한 쓰기 지점 — 그 앞이 유일한 가드 지점이다.
    refusal = None if force else _refusal(
        out_dir, use_catalog=use_catalog, resolve_rate=manifest["totals"]["resolve_rate"]
    )
    if refusal:
        raise GoldFlowBuildRefused(refusal)
    for path, text in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return manifest


def _readme(manifest: dict, unresolved: list[dict]) -> str:
    """산출물 옆에 두는 설명. 수치가 들어가므로 매 빌드에 같이 다시 쓴다 —
    손으로 적어 두면 재생성 후 조용히 거짓말이 된다."""
    t = manifest["totals"]
    rows = "\n".join(
        f"| {e['index']} | {e['bot_name']} | {e['n_steps']} | {e['n_actions']} | "
        f"{e['n_resolved']} | {e['resolve_rate']:.1%} | {e['max_depth']} |"
        for e in manifest["entries"]
    )
    gaps = "\n".join(
        f"| `{u['gold'][0]}/{u['gold'][1]}` | {u['count']} | {u['cases']} |"
        for u in unresolved
    )
    return f"""# 정답흐름도 — 고정된 정답지 (RPA-298 Phase 0-1)

**생성물이다. 손으로 고치지 말 것.** 재생성:

```bash
PYTHONUTF8=1 \\
RAG_DATABASE_URL="postgresql://a360_admin:a360_local_password@localhost:5433/a360" \\
OPENSEARCH_HOST="http://localhost:9201" OPENSEARCH_USERNAME="" OPENSEARCH_PASSWORD="" \\
.venv/Scripts/python.exe -m scripts.goldset_eval.build_gold_flows --goldset <골드셋 루트>
```

## 이게 무엇인가

`정답셋/`의 A360 원본 봇 JSON을 **우리 흐름도 스키마**(`app/schemas/recommendation.py`의
`Recommendation`/`StepRecommendation`/`RecommendedAction`)로 옮기고, 각 액션의 (package,
action)을 **KB 카탈로그 표기로 한 번 풀어서 박아 둔** 것이다.

기존 채점(`metrics.score_case`)은 매 채점마다 토큰 유사도(≥0.55)로 표기를 다시 풀고,
중첩과 파라미터는 아예 보지 않았다. 이 정답지 위에서 도는 `exact_metrics.score_case_exact`는
문자열 일치·중첩 경로·순서를 직접 비교한다. **기존 축을 대체하지 않는다** — 지난 기준선
런이 전부 기존 축으로 재졌으므로 둘이 나란히 나와야 한다.

## 파일

| 파일 | 내용 |
|---|---|
| `cases/<케이스>.json` | 케이스별 흐름도. `steps[].actions[].children[]` 재귀 트리 + `parameters`/`produces`/`consumes`/`kb` |
| `vocabulary.json` | 봇 표기 → KB 표기 **고정 사상표**. `via`(fuzzy/iterator/override/container)·`sim`·동률 후보 포함 |
| `unresolved.json` | 사상 실패 목록 = 현 KB 결손 |
| `manifest.json` | 케이스별 통계·사상률·무결성 플래그 |

## 수치 ({manifest['generated_at']})

- 케이스 **{t['cases']}** · 액션 **{t['actions']}** · 고유 봇 표기 **{t['distinct_gold_notations']}**
- **KB 사상률 {t['resolve_rate']:.1%}** ({t['resolved']}/{t['actions']}) — 미사상 {t['unresolved']}건 / {t['distinct_unresolved']}종
- 카탈로그 {manifest['catalog']['n_action_schemas']}건 · 수동 교정 {manifest['overrides']['n']}건
- 빌더 `{manifest['builder']['version']}` · 변환 규약 지문 `{manifest['builder']['convention_hash']}`
  — 채점기가 이 지문을 현재 코드와 대조해 산출물이 낡았으면 경고한다

| # | 봇 | 단계 | 액션 | 사상 | 사상률 | 최대깊이 |
|---|---|---|---|---|---|---|
{rows}

## 미사상 (KB 결손)

| 봇 표기 | 건수 | 케이스 |
|---|---|---|
{gaps}

## 무결성

`manifest.entries[].sequence_matches_legacy_gold`가 전건 `true`여야 한다. 이 트리를
평탄화한 (package, action) 시퀀스가 기존 `gold.merged_sequence`와 **완전히 같다**는 뜻이고,
그래야 새 축과 옛 축이 같은 정답 모집단을 본다. 회귀는
`tests/test_goldset_flow_schema.py`가 막는다.

## 변환 규약 요약

- `branches`(else/catch/finally) → **다음 형제**로 승격(`branch_of`에 출처 보존)
- 최상위 `Step/step` → `steps[]`, 중첩 `Step/step` → 컨테이너 액션
- `Comment/Comment`·`disabled` 노드(및 하위) 제외 — 뺀 개수는 `stats`에
- Loop는 액션 이름이 아니라 **반복자**로 사상(`Loop/loop.commands.start`는 늘 같은 이름이라)
- 자격증명·비밀 형태 값은 `«redacted»`로 마스킹
"""


def load_gold_flow(out_dir: Path, case_dir: str) -> dict | None:
    """생성된 정답지 한 케이스를 읽는다 (채점기·재채점기가 쓰는 진입점)."""
    p = out_dir / "cases" / f"{case_dir}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


@dataclass
class GoldFlowsStatus:
    """정답지 하나의 신뢰도 판정 — 엄밀 축을 켤지와 사람에게 할 말."""

    dir: Path
    manifest: dict | None = None
    usable: bool = False        # 엄밀 축을 켜도 되는가
    messages: list[str] = field(default_factory=list)


def check_gold_flows(flows_dir: Path) -> GoldFlowsStatus:
    """정답지를 신뢰해도 되는지 판정한다 — `rescore`·`run_eval` 공용.

    manifest **존재**만 보고 엄밀 축을 켜면 안 된다: `--no-catalog` 빌드도 정상적인
    manifest를 남기므로, 전건 unresolved인 정답지 위에서 0에 가까운 그럴듯한 수치가 나온다.
    그래서 (1) 카탈로그 사용 여부와 (2) 사상률을 읽어 **못 믿을 정답지면 축을 끈다.**
    빌더 지문 불일치는 끄지 않고 경고만 한다 — 낡았을 뿐 망가진 건 아니고, 판단은 사람 몫이다.
    """
    st = GoldFlowsStatus(dir=flows_dir)
    st.manifest = read_manifest(flows_dir)
    if st.manifest is None:
        st.messages.append(
            f"ⓘ 고정 정답지 없음({flows_dir}) — 엄밀 축 생략. 생성: "
            f"python -m scripts.goldset_eval.build_gold_flows --goldset <골드셋>")
        return st

    m = st.manifest
    catalog = m.get("catalog") or {}
    totals = m.get("totals") or {}
    rate = totals.get("resolve_rate")

    blockers: list[str] = []
    if catalog.get("used") is False:
        blockers.append("카탈로그 없이 만든 정답지다(`--no-catalog`) — 전건 unresolved라 "
                        "엄밀 축이 0에 가까운 거짓 수치를 낸다")
    elif "used" not in catalog:
        st.messages.append("⚠ 정답지에 카탈로그 사용 여부가 없다(구 산출물) — 재생성 권장")
    if isinstance(rate, (int, float)) and rate < MIN_RESOLVE_RATE:
        blockers.append(f"KB 사상률 {rate:.1%} < {MIN_RESOLVE_RATE:.0%} — 정답지가 망가졌을 "
                        f"가능성이 높다")

    builder = m.get("builder") or {}
    if not builder:
        st.messages.append("⚠ 정답지에 빌더 지문이 없다(구 산출물) — 현재 변환 규약과 "
                           "일치하는지 확인할 수 없다")
    else:
        if builder.get("version") != BUILDER_VERSION:
            st.messages.append(f"⚠ 빌더 세대 불일치: 정답지 {builder.get('version')} ≠ "
                               f"코드 {BUILDER_VERSION} — 재생성 필요")
        if builder.get("convention_hash") != convention_hash():
            st.messages.append("⚠ 변환 규약이 정답지 생성 이후 바뀌었다(지문 불일치) — "
                               "정답지가 낡았을 수 있다. 재생성 권장")

    if blockers:
        st.messages.append(f"⚠ 엄밀 축을 켜지 않는다 ({flows_dir}): " + " / ".join(blockers))
        return st
    st.usable = True
    return st


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--goldset", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-catalog", action="store_true",
                    help="KB 사상 없이 변환만 (DB 없는 환경 확인용 — 정답지로 쓰지 말 것). "
                         "정본 자리에는 --force 없이는 쓰지 못한다")
    ap.add_argument("--force", action="store_true",
                    help="정본 덮어쓰기 가드 해제. 산출물 트리는 git이 아니라 복구 불가 — "
                         "정말 재생성할 때만")
    args = ap.parse_args(argv)

    out = args.out or (args.goldset / DEFAULT_OUT_NAME)
    try:
        m = build(args.goldset, out, use_catalog=not args.no_catalog, force=args.force)
    except GoldFlowBuildRefused as e:
        print(f"⛔ {e}", file=sys.stderr)
        return 2

    t = m["totals"]
    print(f"산출: {out}")
    print(f"  케이스 {t['cases']} · 액션 {t['actions']} · 고유 봇 표기 {t['distinct_gold_notations']}"
          f" · 사상 행 {t['distinct_mappings']}")
    print(f"  KB 사상 {t['resolved']}/{t['actions']} = {t['resolve_rate']:.1%}"
          f" (미사상 {t['unresolved']}건 / 고유 {t['distinct_unresolved']}종)")
    drifted = [e for e in m["entries"] if not e["sequence_matches_legacy_gold"]]
    if drifted:
        print(f"  ⚠ 기존 gold.merged_sequence와 어긋난 케이스 {len(drifted)}건: "
              + ", ".join(str(e["index"]) for e in drifted), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
