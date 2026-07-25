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

# Bot Store 제출 규약 보일러플레이트 Step 제목 (소문자 비교).
#
# 왜 표시하나: 골드셋 봇은 Bot Store(현 Agentic App Store)에 올라온 것이라, 마켓 심사
# 요건을 맞추려고 벤더들이 공용 템플릿을 쓴다. 실측 5건(02·03·05·09·12)이 주석 문구까지
# 동일하고, 템플릿 안에 "*********Bot Logic Goes Inside Here*************" 주석으로
# 업무 로직 자리를 표시해 둔다 — 그 바깥은 전부 제출용 껍데기다.
#
# 왜 문제인가: 업무정의서에도 공식 문서에도 없고 추론도 불가능하다("30일"은 그 템플릿의
# 임의 관습이다). 그런데 n_gold에 포함돼 재현율 천장을 낮춘다 — 실측 496개 중 180개(36%),
# 케이스 05는 33개 중 30개가 이것이라 최선을 다해도 재현율 0.09가 상한이었다.
#
# 접착제(폴더 확인·경로 조립·로깅)와는 구분한다. 접착제는 "이 업무를 돌리려면 필요하다"에서
# 도출 가능해 목표에 포함되지만, 제출 규약은 근거가 어디에도 없다.
BOILERPLATE_STEP_TITLES = frozenset({
    "verify bot name and vendor name",
    "setup log folders/locations",
    "clear out old logs",
    "log error",
    "record snapshot",
    "writing to log file",
    "data manipulation",
})

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
    # sequence와 같은 길이·순서. True면 그 액션이 Bot Store 제출 규약 보일러플레이트다.
    boilerplate: list[bool] = field(default_factory=list)


def _step_title(node: dict) -> str | None:
    """Step 노드의 title 속성 (소문자·trim). 없으면 None."""
    for attr in node.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("name") == "title":
            val = attr.get("value")
            if isinstance(val, dict) and isinstance(val.get("string"), str):
                return val["string"].strip().lower()
    return None


def _walk(nodes: list[dict], flow: GoldFlow, depth: int, in_boiler: bool = False) -> None:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("disabled"):
            flow.disabled_count += 1
            continue
        pkg = node.get("packageName")
        cmd = node.get("commandName")
        # 보일러플레이트 Step 안에 들어가면 그 하위 전체가 보일러플레이트다.
        here = in_boiler or (
            (pkg, cmd) == ("Step", "step") and _step_title(node) in BOILERPLATE_STEP_TITLES
        )
        if pkg and cmd:
            key = _CONTAINER_KEYS.get(cmd)
            if key:
                flow.structure[key] = flow.structure.get(key, 0) + 1
            if (pkg, cmd) not in SCAFFOLD:
                flow.sequence.append((pkg, cmd))
                flow.boilerplate.append(here)
            flow.structure["max_depth"] = max(flow.structure.get("max_depth", 0), depth)
            # 루프 iterator 종류 기록 (attributes 안의 ITERATOR value)
            for attr in node.get("attributes") or []:
                val = attr.get("value") if isinstance(attr, dict) else None
                if isinstance(val, dict) and val.get("type") == "ITERATOR":
                    flow.iterators.append(f"{val.get('packageName')}/{val.get('iteratorName')}")
        _walk(node.get("children") or [], flow, depth + 1, here)
        _walk(node.get("branches") or [], flow, depth + 1, here)


def load_gold_flow(path: Path) -> GoldFlow:
    doc = json.loads(path.read_text(encoding="utf-8"))
    flow = GoldFlow(source_file=path.name)
    _walk(doc.get("nodes") or [], flow, 1)
    flow.structure["variables"] = len(doc.get("variables") or [])
    flow.structure["actions"] = len(flow.sequence)
    flow.structure["boilerplate_actions"] = sum(flow.boilerplate)
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


def merged_boilerplate(flows: list[GoldFlow]) -> list[bool]:
    """merged_sequence와 같은 길이·순서의 보일러플레이트 표시."""
    flags: list[bool] = []
    for f in flows:
        flags.extend(f.boilerplate)
    return flags
