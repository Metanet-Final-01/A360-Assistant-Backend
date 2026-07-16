"""골드셋 정답 봇 JSON → 정규 액션 시퀀스 추출.

정답셋은 A360 원본 봇 JSON(nodes[].commandName/packageName, 재귀 children/branches)이다.
disabled=true 노드는 실행되지 않으므로 제외한다. branches(try의 catch 등)는 children
다음에 pre-order로 잇는다 — 실행 구조상 본문 뒤 핸들러가 오는 순서와 일치한다.

Step/step은 순수 구획(스캐폴딩)이라 액션 지표에서 제외하고 구조 지표로만 센다 —
에이전트 Recommendation에서는 StepRecommendation(steps[])이 그 역할을 하므로
액션 대 액션으로 비교하면 한쪽만 불리해진다.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

# 액션 지표에서 제외하는 비실행 노드 (구조 지표로만 집계)
# - Step/step: 순수 구획 — 에이전트에선 StepRecommendation(steps[])이 대응
# - Comment/Comment: 주석 — 실행 의미 없음 (골드셋에 125회 등장, 지표 오염 방지)
SCAFFOLD = {("Step", "step"), ("Comment", "Comment")}

# 구조 지표로 세는 컨테이너 commandName (packageName 무관 집계 키)
_CONTAINER_KEYS = {
    "step": "step",
    "loop.commands.start": "loop",
    "try": "try",
    "catch": "catch",
    "finally": "finally",
    "if": "if",
    "else": "else",
    "elseif": "elseif",
    "elseIf": "elseif",
    "Comment": "comment",
}


@dataclass
class GoldFlow:
    """정답 봇 하나의 정규화 산출물."""

    source_file: str
    sequence: list[tuple[str, str]] = field(default_factory=list)  # 스캐폴딩 제외 pre-order
    structure: dict = field(default_factory=dict)  # container 카운트·깊이·변수 수
    disabled_count: int = 0
    iterators: list[str] = field(default_factory=list)  # 루프 iterator (packageName/iteratorName)


def _walk(nodes: list[dict], flow: GoldFlow, depth: int) -> None:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("disabled"):
            flow.disabled_count += 1
            continue
        pkg = node.get("packageName")
        cmd = node.get("commandName")
        if pkg and cmd:
            key = _CONTAINER_KEYS.get(cmd)
            if key:
                flow.structure[key] = flow.structure.get(key, 0) + 1
            if (pkg, cmd) not in SCAFFOLD:
                flow.sequence.append((pkg, cmd))
            flow.structure["max_depth"] = max(flow.structure.get("max_depth", 0), depth)
            # 루프 iterator 종류 기록 (attributes 안의 ITERATOR value)
            for attr in node.get("attributes") or []:
                val = attr.get("value") if isinstance(attr, dict) else None
                if isinstance(val, dict) and val.get("type") == "ITERATOR":
                    flow.iterators.append(f"{val.get('packageName')}/{val.get('iteratorName')}")
        _walk(node.get("children") or [], flow, depth + 1)
        _walk(node.get("branches") or [], flow, depth + 1)


def load_gold_flow(path: Path) -> GoldFlow:
    doc = json.loads(path.read_text(encoding="utf-8"))
    flow = GoldFlow(source_file=path.name)
    _walk(doc.get("nodes") or [], flow, 1)
    flow.structure["variables"] = len(doc.get("variables") or [])
    flow.structure["actions"] = len(flow.sequence)
    return flow


def load_case(case_dir: Path) -> list[GoldFlow]:
    """케이스 폴더의 workflows/*.json 전부 (멀티봇 케이스: 메인+서브태스크)."""
    flows = []
    for p in sorted((case_dir / "workflows").glob("*.json")):
        flows.append(load_gold_flow(p))
    return flows


def merged_sequence(flows: list[GoldFlow]) -> list[tuple[str, str]]:
    """멀티봇 케이스의 정답 시퀀스 병합 — 파일명 순(메인 봇이 먼저 오도록 정렬돼 있음)."""
    seq: list[tuple[str, str]] = []
    for f in flows:
        seq.extend(f.sequence)
    return seq
