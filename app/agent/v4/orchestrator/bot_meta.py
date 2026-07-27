# -*- coding: utf-8 -*-
"""봇 저장 메타 채우기 — 이름은 제안, 나머지는 결정론 (설계 제약 #15).

## 왜 이게 필요한가

완성도 범위(제약 #15)는 액션 + **변수 설계 · 트리거 · 오류 처리 구조 · 봇 메타**다.
앞의 셋은 각각 `produces/consumes`(R9~R11) · `TriggerRecommendation`(A-2) ·
`Error handler` 구조 검사(R13)로 들어와 있었는데, **봇 메타만 담을 자리가 없었다** —
`Recommendation` 스키마 최상위에 필드 자체가 없었다.

우리 정답 기준은 "비전문가가 흐름도를 보고 Control Room에 손으로 넣으면 **돌아간다**"이다.
그 사람이 제일 먼저 만나는 건 액션이 아니라 **저장 대화상자**(이름·폴더)다. 거기서 막히면
흐름도가 아무리 정확해도 봇이 안 만들어진다.

## 지어내지 않는다

계획서는 "Examples 문서 Procedure의 첫 단계가 정확히 이 내용이라 용례 채널에서 자연히
따라온다"고 봤지만 **데이터가 그렇지 않았다**(2026-07-27 실측):

- 정답 봇 JSON 최상위 키: `breakpoints/nodes/packages/triggers/variables/workItemTemplateName`
  — 이름은 파일명에서 오고 폴더·플랫폼은 **아예 없다.**
- 용례 34건의 첫 단계는 봇 생성 절차가 아니라 실제 액션(`Browser/Open`, `Dictionary/Put` …)이다.
  애초에 용례 빌더가 봇 생성 보일러플레이트를 걷어냈다(그걸 지키는 테스트도 있다).
- 공식 문서 코퍼스에 "봇을 어떻게 이름 짓고 어디 두는가" 페이지가 없다 — 검색하면
  릴리스 노트와 무관한 스캔 도구 페이지만 나온다.

그래서 근거가 있는 것만 값으로 내고 나머지는 자리표시자로 둔다(설계 §5.2-G).

| 필드 | 출처 | 근거 |
|---|---|---|
| `name` | LLM 제안 | 사람이 바꿔도 그만이라 지어내도 손해가 없는 유일한 항목 |
| `folder` | 자리표시자 | 사용자 작업공간 경로 = 업무 데이터(제약 #10). 추측하면 없는 경로를 확신 있게 적는다 |
| `target_os` | `checker.target_os(flow)` | R16이 이미 쓰는 판단 — 따로 읽으면 경고와 출력이 어긋난다 |
| `run_mode` | `flow["trigger"]` 유무 | R15가 "트리거 있으면 사실상 무인 실행"으로 이미 쓰는 판단 |
"""

from ..verify.checker import target_os

# 폴더 자리표시자 — 값이 아니라 **빈칸이라는 사실**을 사람에게 전달하는 문구다.
# 그럴듯한 경로(`Bots/Finance`)를 적으면 사용자가 그대로 옮겨 존재하지 않는 폴더를 만든다.
FOLDER_PLACEHOLDER = "‹저장할 폴더를 지정하세요›"

_MAX_NAME_LEN = 80


def _clean_name(raw) -> str | None:
    """LLM이 준 이름을 다듬는다 — 못 쓸 값이면 None(아래에서 목표 문장으로 폴백)."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())
    # 자리표시자 기호를 그대로 돌려주는 경우가 있다 — 이름 자리에 들어가면 파일명이 깨진다.
    if not name or name.startswith("‹") or name.lower() in ("null", "none", "미정"):
        return None
    return name[:_MAX_NAME_LEN]


def _name_from_goal(spec: dict | None) -> str | None:
    """이름이 비면 spec의 목표 문장에서 만든다 — 빈칸보다 낫고, 사람이 바꾸면 그만이다."""
    if not isinstance(spec, dict):
        return None
    goal = spec.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return None
    return " ".join(goal.split())[:_MAX_NAME_LEN]


def fill_bot_meta(flow: dict, *, trust_folder: bool = False) -> dict:
    """흐름도의 `bot_meta`를 제자리에서 채워 넣고 그 dict를 돌려준다.

    LLM이 `name`을 줬으면 존중하고, 나머지 세 필드는 **항상 덮어쓴다** — 결정론 판단이라
    LLM 값이 있어도 그걸 믿을 이유가 없고, 믿으면 R15/R16 경고와 어긋날 수 있다.

    `trust_folder`가 폴더의 출처를 가른다. **문자열만 봐서는 LLM이 지어낸 경로와 사용자가
    정한 경로를 구분할 수 없다** — 둘 다 그냥 문자열이다. 그래서 값이 아니라 **호출 경로**로
    가른다:

      - 생성(`generate`) — `trust_folder=False`(기본). 이 경로의 `bot_meta`는 방금 LLM이
        만든 것이라 폴더를 믿을 근거가 0이다. 무조건 자리표시자로 되돌린다.
      - 편집(`edit`)     — `trust_folder=True`. 이 경로의 `bot_meta`는 저장된 추천안에서
        오므로, 자리표시자가 아닌 값이 들어 있다면 사람이 넣은 것이다.

    ⚠️ 이 구분은 "저장된 값 = 사람이 넣은 값"이라는 전제 위에 있다. edit이 폴더를 LLM으로
    채우는 연산을 갖게 되면 전제가 깨지므로, 그때는 값에 출처 표시(`value_source` 같은)를
    붙여야 한다. 지금은 그런 연산이 없다.

    흐름도에 액션이 하나도 없으면 아무것도 하지 않는다(빈 껍데기에 메타만 붙는 걸 막는다).
    """
    if not isinstance(flow, dict) or not flow.get("steps"):
        return flow

    meta = flow.get("bot_meta")
    if not isinstance(meta, dict):
        meta = {}

    meta["name"] = _clean_name(meta.get("name")) or _name_from_goal(flow.get("spec"))
    folder = meta.get("folder")
    keep = (
        trust_folder
        and isinstance(folder, str)
        and folder.strip()
        and not folder.startswith("‹")
    )
    meta["folder"] = folder if keep else FOLDER_PLACEHOLDER
    meta["target_os"] = target_os(flow)
    meta["run_mode"] = "unattended" if flow.get("trigger") else "attended"

    flow["bot_meta"] = meta
    return flow
