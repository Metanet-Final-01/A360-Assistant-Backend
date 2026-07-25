"""A360 공식 문서의 '자동화 용례' 34건 → 에이전트 few-shot 자산(JSON) 오프라인 배치 (RPA-298).

왜 이 자산이 필요한가 — **용례 슬롯이 비어 있다.**
흐름 생성 에이전트의 검색은 source_type을 ["action_schema", "bot_example"]로 좁혀 두는데
`bot_example`은 코퍼스에 **0건**이다. 즉 에이전트가 보는 것은 액션 스펙 1,200개(= 단어장)뿐이고
"이 업무에는 이런 액션들을 이런 순서·이런 파라미터로 엮는다"는 **문장(용례)을 한 번도 못 본다**.
실측 재현율 0.236의 주원인으로 지목된 지점이다.

그 빈 슬롯을 채울 재료가 이미 코퍼스에 있다: `rag_documents`의 doc_page 중
breadcrumbs에 "Examples of building automations"가 달린 34개 튜토리얼(EN/KO 번역쌍)이다.
이 스크립트가 그걸 **오프라인에서 1회** 결정론적으로 파싱해 JSON 자산으로 굳힌다.

런타임 계약: 소비부는 이 JSON **파일만** 읽는다 — DB 조회 0회, LLM 0회.
대상이 34건뿐이라 검색기를 태울 이유가 없다(희귀 소스타입은 하이브리드 상위 k에서 굶는다는
trigger_schema 실측과 같은 이유. app/services/catalog.list_trigger_schemas 주석 참고).

설계 원칙 — **추측 금지**:
  · 정본은 locale='en-US'만. ko-KR은 축약·완전 한글화라 카탈로그(영문 액션명) 대응이 불가능하다.
    실측: KO는 34문서/101청크로 EN 158청크의 64%밖에 안 된다 = 본문이 잘려 있다. KO는 제목만 병기.
  · 추출한 (package, action)은 전부 `get_backend_catalog().get_action_schema()`로 실재를 검증한다.
    **카탈로그에 없으면 자산에서 뺀다** — 존재하지 않는 액션을 few-shot으로 주면 R1 환각을 학습시킨다.
  · 별칭표(_ACTION_ALIASES / _PACKAGE_ALIASES)는 전부 실측 근거를 주석에 달았다. 근거 없는 항목 금지.

사용법:
    # 반드시 로컬 코퍼스를 가리켜라 — .env의 RAG_DATABASE_URL은 운영 Neon이다
    RAG_DATABASE_URL="postgresql://a360_admin:a360_local_password@localhost:5433/a360" \
        python scripts/agent_v4/build_examples.py --dry-run
    ... --verify-catalog     # 자산의 모든 (pkg, act) 실재 검증. 미해석 0건이어야 한다
    ... --holdout-audit      # 자산 본문 ↔ 골드셋 업무정의서 4-gram 교집합
    ...                      # 인자 없음 = JSON 생성
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Windows 콘솔 기본 cp949로는 한국어 주석/근거 출력이 UnicodeEncodeError로 죽는다 —
# 이 스크립트의 리포트는 전부 한국어라 필수다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # 파이프 리다이렉트 등
        pass

OUT_PATH = ROOT / "app" / "agent" / "knowledge" / "examples" / "data" / "automation_examples.json"

# 골드셋 업무정의서(정규화) 13건 — 홀드아웃 판정의 비교 대상.
# 리포 밖 경로다: 골드셋은 평가 자산이라 제품 리포에 커밋하지 않는다(누출 방지).
GOLDSET_DIR = Path(r"C:\Users\qoqkd\Desktop\final-etc-files\골드셋\업무정의서_정규화")

SCHEMA_VERSION = 1
SOURCE_LABEL = "docs.automationanywhere.com — Build automations > Task Bots > Examples of building automations"

# 대상 문서를 고르는 breadcrumb. metadata.breadcrumbs = ["Build automations","Task Bots","Examples of building automations"]
BREADCRUMB = "Examples of building automations"


# ---------------------------------------------------------------------------
# 0. DB 접근
# ---------------------------------------------------------------------------
# ⚠️ scripts/ 아래는 app/core/config.py 경유 래칫(직접 getenv 금지)의 대상이 아니다.
#    여기서 굳이 직접 읽는 이유: 이 배치는 **운영이 아니라 로컬 코퍼스**를 대상으로 도는데,
#    config.REGISTRY를 거치면 .env(운영 Neon)가 먼저 잡혀 조용히 운영 DB를 훑게 된다.
#    호출자가 셸에서 준 값만 쓰도록 강제하고, 없으면 실행을 거부한다.
def _dsn() -> str:
    dsn = os.environ.get("RAG_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit(
            "RAG_DATABASE_URL이 비어 있다. 로컬 코퍼스 DSN을 명시적으로 주고 실행하라:\n"
            '  RAG_DATABASE_URL="postgresql://a360_admin:a360_local_password@localhost:5433/a360"'
        )
    return dsn


def _fetch_docs() -> list[dict]:
    """대상 doc_page를 청크 단위로 전부 가져온다 (EN/KO 모두)."""
    import psycopg

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, COALESCE(parent_id, id) AS pid, chunk_index, locale, title, url, content
            FROM rag_documents
            WHERE source_type = 'doc_page'
              AND metadata->'breadcrumbs' @> %s::jsonb
            ORDER BY locale, pid, chunk_index
            """,
            (json.dumps([BREADCRUMB]),),
        )
        cols = ["id", "pid", "chunk_index", "locale", "title", "url", "content"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1. 청크 병합 (overlap 재수록 제거)
# ---------------------------------------------------------------------------
# 수집 파이프라인이 chunk_size=1200 / chunk_overlap=120으로 잘랐고(metadata 실측),
# 2번째 청크부터는 머리에 `<제목>` + `(이어짐: n/m 조각)` 배너를 달고 **직전 청크의 꼬리를 재수록**한다.
# 이걸 안 지우면 같은 액션 라인이 2~3회 잡혀 steps가 부풀고(실측: reading-spreadsheet 문서에서
# `Excel advanced > Open`이 2회, `Go to cell`이 5회) few-shot이 "같은 액션을 반복하라"를 가르친다.
_CONT_BANNER = re.compile(r"^\(.*?:\s*\d+\s*/\s*\d+.*?\)$")
# 재수록 구간은 120자라 줄 수로는 최대 3~4줄. 여유를 둬 8줄까지 본다.
_MAX_OVERLAP_LINES = 8


def _merge_chunks(chunks: list[dict]) -> list[str]:
    """chunk_index 순 병합 + 배너/재수록 줄 제거 → 정규화된 줄 목록."""
    chunks = sorted(chunks, key=lambda c: c["chunk_index"])
    title = chunks[0]["title"]
    acc: list[str] = []
    for c in chunks:
        lines = [ln.strip() for ln in (c["content"] or "").split("\n")]
        if c["chunk_index"] > 0:
            # 배너(제목 + `(이어짐: n/m 조각)`) 제거
            while lines and (not lines[0] or _CONT_BANNER.match(lines[0]) or lines[0] == title):
                lines.pop(0)
        lines = [ln for ln in lines if ln]
        if acc:
            # 꼬리 k줄 == 새 머리 k줄이면 그 k줄이 overlap 재수록이다. 긴 쪽부터 맞춰본다.
            for k in range(min(len(lines), _MAX_OVERLAP_LINES), 0, -1):
                if acc[-k:] == lines[:k]:
                    lines = lines[k:]
                    break
        acc.extend(lines)
    return acc


# ---------------------------------------------------------------------------
# 2. 섹션 분할
# ---------------------------------------------------------------------------
# 실측으로 확인: 이 헤더들은 **독립 줄**로 존재한다(34문서 중 Procedure 33 / About this task 24 /
# Before you begin 23 / Related reference 12 / Results 2 / Example 2 / What to do next 1).
# Procedure가 없는 유일한 문서는 인덱스 페이지 "Common automation tasks and examples"다.
_SECTION_HEADS = {
    "Before you begin": "before",
    "About this task": "about",
    "Procedure": "procedure",
    "Results": "results",
    "Example": "example",
    "What to do next": "after",
    # 아래는 문서 말미의 링크 목록 — 절차가 아니므로 여기서 잘라 낸다.
    "Related reference": "trailer",
    "Related concepts": "trailer",
    "Related tasks": "trailer",
    "Related information": "trailer",
}
_TRAILER = "trailer"


def _split_sections(lines: list[str], title: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    cur = "lead"
    for ln in lines:
        if ln in _SECTION_HEADS:
            cur = _SECTION_HEADS[ln]
            continue
        # 머리말: 제목 반복 + breadcrumb 줄은 본문이 아니다.
        if ln == title or ln.startswith("Build automations >"):
            continue
        out[cur].append(ln)
    return out


# ---------------------------------------------------------------------------
# 3. 보일러플레이트 (봇 생성 UI 절차)
# ---------------------------------------------------------------------------
# 모든 튜토리얼이 같은 "봇 만들기" 서두를 반복한다. few-shot에는 무가치하고(에이전트는 흐름을
# 설계하지 Control Room UI를 클릭하지 않는다) 오히려 의도 라인 후보를 오염시킨다.
# 실측: 34문서 2,920줄 중 아래 규칙에 걸리는 줄이 **250줄(8.6%)**.
_BOILERPLATE = re.compile(
    r"^(?:"
    r"On the left (?:panel|pane), click Automation"
    r"|Click (?:\+ )?Create ?(?:new|New)? *> *(?:Task Bot|Bot)\b"
    r"|Click Create and [Ee]dit"
    r"|Click Create ?& ?Edit"
    r"|Click the Create a bot icon"
    r"|Create a (?:new )?bot\s*\.?$"
    r"|Create a new [Tt]ask ?[Bb]ot\s*\.?$"
    r"|Open a new bot\s*\.?$"
    r"|In the Create Task Bot window"
    r"|Enter a bot name"
    r"|Accept the default folder location"
    r"|Log in to (?:the )?Control Room"
    r"|Run the bot\s*\.?$"
    # 수집기가 "bot" 토큰을 지운 흔적("Run the to test the connection.") 포함 — 의도 라인 오염 방지
    r"|Run the\b.{0,24}\bto test\b"
    r"|Save the changes\s*\.?$"
    r"|Save changes\s*\.?$"
    r"|Click Save\s*\.?$"
    r"|Click Save and (?:close|run)"
    r"|Select the Save changes option"
    # 영상 캡션 — 절차가 아니라 "이 영상은 …을 보여줍니다"다. 실측 9줄.
    # 액션 이름을 그대로 다시 부르기 때문에("This video shows how to use the Add column action …")
    # 안 지우면 G5가 유령 스텝을 만든다(실측: Salesforce 문서에서 Add column·Execute SOQL 중복).
    r"|This video shows"
    r")",
)


# ---------------------------------------------------------------------------
# 4. 액션 라인 문법
# ---------------------------------------------------------------------------
# 문서가 액션 추가를 표현하는 문형은 실측 4가지다. G1이 압도적(195회)이고 나머지는 소수 문서 한정.
#
# G1 `... <Pkg> > <Action> action ...`
#     예: "From the Actions panel, find and add the Excel advanced > Open action to the Bot editor ."
#     ⚠️ 왼쪽이 "Double-click or drag the Excel advanced"처럼 **동사구가 붙어 온다**. 그래서
#        `>` 앞을 통째로 패키지로 읽으면 안 되고, **카탈로그 패키지명(120개)의 최장 접미 일치**로 끊는다.
#        이 방식이 "Go to Actions > Google Drive" 류 UI 내비게이션도 자동으로 배제한다 —
#        "Actions"는 카탈로그 패키지가 아니고, `>` 뒤가 "... action"으로 끝나지도 않는다.
# G2 `Go to Actions > <Pkg> ..., and double-click [or drag] [the] <Action> [action]`
#     실측 2문서(file-variable-streaming, json-utilities)만 이 문형을 쓴다. G1이 이 문서들을
#     전부 놓치므로 별도로 받는다.
# G3 `find the <Pkg> package from the Actions panel, and add the <Action> action`
#     실측 1문서(file variable). 패키지가 문장 앞쪽에 따로 나온다.
# G4 제어 흐름 무패키지 문형 — "Double-click or drag the Loop action", "Drag the Message box action".
#     실측: Loop 35 / Message box 21 / If 15 / Try 2 / Else 1회 언급. 단 **대부분이 위치 지시**
#     ("within the Loop action", "after the If action")라 추가 동사가 앞에 붙은 것만 받는다.
# G5 무패키지 + 문서 문맥 패키지 — "Use the Create function action", "Select the Get table action".
#     실측: SAP BAPI 3문서가 첫 액션만 `SAP BAPI > Connect`로 쓰고 이후는 패키지를 생략한다.
#     G5가 없으면 이 문서들의 액션 6~8개를 통째로 놓치고, 더 나쁘게는 **뒤따르는 파라미터 라인이
#     엉뚱한 직전 스텝에 붙는다**(실측: `Data Table > Write to file`에 'BAPI function alias' 7회).
#     해석 근거를 문서가 이미 도입한 패키지로 한정하고 **후보가 정확히 1개일 때만** 받는다(추측 금지).
_ARROW = re.compile(r"([^>\n]+?)\s*>\s*([A-Za-z][\w ./&'-]*?)\s+action\b")

_G2 = re.compile(
    r"Go to Actions\s*>\s*([\w ./&-]+?)\s*(?:on the left pane)?\s*,\s*and\s+"
    r"double[- ]?click(?:\s+or\s+drag)?\s+(?:the\s+)?([A-Za-z][\w ./&'-]*?)\s*"
    r"(?:action\b|to add)",
    re.I,
)
_G3 = re.compile(
    r"find the\s+([\w ./&-]+?)\s+package\s+from the Actions panel\s*,\s*and\s+add the\s+"
    r"([A-Za-z][\w ./&'-]*?)\s+action\b",
    re.I,
)
# G4/G5 공통 머리 — 추가 동사 + 관사. 위치 지시(within/after/inside/…)는 _POSITIONAL이 거른다.
_ADD_VERB = (r"(?:\bdouble[- ]?click or drag\b|\bdouble[- ]?click\b|\bdrag\b|\bfind and add\b"
             r"|\badd\b|\binsert\b|\buse\b|\busing\b|\bselect\b)")
_G4 = re.compile(
    _ADD_VERB + r"\s+(?:the|a|an)\s+"
    r"(Loop|If|Else|Try|Catch|Finally|Message ?[Bb]ox|Step|Delay|SOAP web services?)\s+action\b",
    re.I,
)
# 동사부만 대소문자 무시(`(?i:…)`), 액션명은 **대문자 시작 강제**.
# 후자를 풀면 "use the same action", "drag the required action" 같은 일반 명사구가 액션으로 잡힌다.
_G5 = re.compile(r"(?i:" + _ADD_VERB + r"\s+(?:the|a|an))\s+([A-Z][\w ./&'-]{2,40}?)\s+action\b")
# 위치 지시/참조로 쓰인 언급 — 이 앞말이 붙으면 '액션 추가'가 아니라 '어디에 넣어라 / 아까 그것'이다.
# G1에도 반드시 적용해야 한다. 실측(VBScript 문서): "Double-click or drag the String > To number
# action , adding it as the last line before the Error handler > Catch action ." 처럼 **한 줄에
# 추가 1개 + 위치 참조 1개**가 같은 화살표 문법으로 나온다 → 안 거르면 Error handler > Catch가
# 그 문서에서만 6번 스텝으로 들어간다.
_POSITIONAL = re.compile(
    r"\b(?:within|inside|after|before|next to|with|into|outside|under|to|in|of)\s+the\s+$", re.I
)

# G4의 무패키지 이름 → 카탈로그 (package, action).
# 근거: 카탈로그 실측 — If::['Else','Else if (optional)','If'] / Error handler::['Catch','Finally','Throw','Try']
#       / Message Box::['Message box'] / Step::['Step'] / Delay::['Delay'] / Loop은 별칭 해석 경유.
_BARE_ACTION_PKG = {
    "loop": ("Loop", "Loop"),
    "if": ("If", "If"),
    "else": ("If", "Else"),
    "try": ("Error handler", "Try"),
    "catch": ("Error handler", "Catch"),
    "finally": ("Error handler", "Finally"),
    "message box": ("Message Box", "Message box"),
    "messagebox": ("Message Box", "Message box"),
    "step": ("Step", "Step"),
    "delay": ("Delay", "Delay"),
    # 근거: 카탈로그 'SOAP Web Service' 패키지의 액션은 'SOAP web service' 단 1개.
    #       문서(soap-web-service 튜토리얼)는 패키지 없이 복수형 "SOAP web services action"으로 쓴다.
    "soap web service": ("SOAP Web Service", "SOAP web service"),
    "soap web services": ("SOAP Web Service", "SOAP web service"),
}


def _in_parenthetical(prefix: str) -> bool:
    """매치 지점이 괄호 안인가.

    문서는 파라미터 라인 안에서 다른 액션을 **참조**한다:
      "In the BAPI function alias field, enter BAPI_POST (the alias you provided ... using the
       Create function action )."
    이걸 액션 추가로 읽으면 존재하지 않는 스텝이 생기고 그 뒤 파라미터가 전부 그리로 딸려간다.
    """
    return prefix.count("(") > prefix.count(")")


# ---------------------------------------------------------------------------
# 5. 파라미터 문형
# ---------------------------------------------------------------------------
# 실측 전수 조사(34문서 2,920줄)로 추린 문형. 순서가 곧 우선순위다 — 앞 패턴이 먼저 먹는다.
# (필드, 값) 두 조각만 뽑고, 필드명은 뒤에서 **카탈로그 스펙의 파라미터 이름으로 정규화**한다.
_PARAM_PATTERNS: list[tuple[str, re.Pattern]] = [
    # "In the URL field, enter https://..." / "In Session name , enter orderlist ."
    # `type:`도 받는다 — Message box 튜토리얼이 "…field, type: From the DLL: …"로 쓴다(실측).
    ("In X, enter Y", re.compile(r"^In (?:the )?(.+?)\s*(?:field|drop-down list|drop-down|list|option|box)?\s*,\s*(?:enter|type:?)\s+(.+?)\s*\.?$", re.I)),
    # "In Loop through , select All rows ."
    ("In X, select Y", re.compile(r"^In (?:the )?(.+?)\s*(?:field|drop-down list|drop-down|list|option|box)?\s*,\s*select\s+(.+?)\s*\.?$", re.I)),
    # "In Create Excel session , click Local session , and enter orderlist as the session name."
    ("In X, click Y", re.compile(r"^In (?:the )?(.+?)\s*(?:field|drop-down list|drop-down|list|option|box)?\s*,\s*click\s+(.+?)\s*\.?$", re.I)),
    # "From Iterator , select For each row in worksheet for Excel advanced ."
    ("From X, select Y", re.compile(r"^From (?:the )?(.+?)\s*(?:field|drop-down list|drop-down|list)?\s*,\s*select\s+(.+?)\s*\.?$", re.I)),
    # "Select vSourceDictionary from the Value type field."
    ("Select Y from the X", re.compile(r"^Select\s+(?P<val>.+?)\s+from the\s+(?P<fld>.+?)\s*(?:field|drop-down list|drop-down|list)\s*\.?$", re.I)),
    # "Enter tags in the Key field."
    ("Enter Y in X", re.compile(r"^Enter\s+(?P<val>.+?)\s+in\s+(?:the\s+)?(?P<fld>.+?)\s+field\s*\.?$", re.I)),
    # "Select Specific sheet name and enter order_list ." → 옵션 선택 + 값. 아래 "Select X." 보다 먼저 봐야
    # 값(order_list)을 잃지 않는다.
    ("Select X and enter Y", re.compile(r"^Select\s+(?P<fld>[A-Z][\w /$-]{2,45}?)\s+and (?:then )?enter\s+(?P<val>.+?)\s*\.?$")),
    # "Select the Sheet contains a header check box." → 불리언 on
    ("Select the X check box", re.compile(r"^Select the\s+(.+?)\s+check ?box\s*\.?$", re.I)),
    # "Select Sheet contains a header ." → 불리언 on (체크박스 라벨 단독)
    ("Select X.", re.compile(r"^Select\s+(?P<fld>[A-Z][\w /$-]{2,45})\s*\.$")),
    # "In the Assign value to the variable field, create the variable HeaderData ."
    ("In X, create/specify Y", re.compile(r"^In (?:the )?(.+?)\s*(?:field)?\s*,\s*(?:create|specify|provide|assign)\s+(.+?)\s*\.?$", re.I)),
    # "Click File and select the sample Excel file you downloaded."
    ("Click X and select Y", re.compile(r"^Click\s+(?:the\s+)?(.+?)\s+and\s+(?:then\s+)?select\s+(.+?)\s*\.?$", re.I)),
]

# 의도 라인 후보에서 제외할 머리말 — 이걸로 시작하면 '업무 의도'가 아니라 UI 조작/부연이다.
# (파라미터 문형으로 잡히지 않은 UI 지시가 의도 자리에 들어가면 few-shot의 설명이 통째로 망가진다.
#  실측: "In Name , enter Get Sales Opportunities ." 가 Salesforce Authentication의 의도로 붙었다.)
_NOT_INTENT = re.compile(
    r"^(?:Click|Select|Enter|Choose|Press|Provide|Ensure|Make sure|Note\s*:|Optional\s*:|Repeat|"
    r"In\s|From\s|On the|Go to|Navigate|Expand|Type|Verify|Search|Drag|Double|Add\s|Use\s|Find\s)",
    re.I,
)
# 값 없이 필드만 잡히는 문형(체크박스) — 값을 "on"으로 고정한다.
_BOOLEAN_PATTERNS = {"Select the X check box", "Select X."}
# 파라미터 값 최대 길이(문자). 초과분은 잘라낸다.
_MAX_PARAM_VALUE = 100


def _param_from_line(line: str) -> tuple[str, str, str] | None:
    """(문형이름, 필드, 값). 매칭 실패면 None."""
    for name, pat in _PARAM_PATTERNS:
        m = pat.match(line)
        if not m:
            continue
        if name in _BOOLEAN_PATTERNS:
            fld = m.groupdict().get("fld") or m.group(1)
            return name, fld.strip(" ,."), "on"
        gd = m.groupdict()
        if "fld" in gd and "val" in gd:
            fld, val = gd["fld"], gd["val"]
        else:
            fld, val = m.group(1), m.group(2)
        fld = fld.strip(" ,.")
        val = val.strip(" ,.")
        if not fld or not val or len(fld) > 60:
            return None
        # 문서는 값 뒤에 설명을 이어 붙인다("… enter A2 in Cell name, which is the first data row").
        # 값이 길어지면 few-shot이 값 대신 산문을 흉내 낸다 — 실측 최장 유효값이 60자 언저리라 넉넉히 자른다.
        if len(val) > _MAX_PARAM_VALUE:
            val = val[:_MAX_PARAM_VALUE].rstrip() + "…"
        return name, fld, val
    return None


# ---------------------------------------------------------------------------
# 6. 카탈로그 정규화
# ---------------------------------------------------------------------------
# 문서 표기 → 카탈로그 표기 별칭. **전부 실측 근거를 달았다.**
_PACKAGE_ALIASES = {
    # 문서는 "JSON" / "Json" / "JSON Utilities"로 쓰는데 카탈로그 패키지는 'JSON utilities' 하나뿐.
    # 근거: 문서가 이 패키지로 부르는 액션(Start session/End session/Get node value/Update node value/
    #       Add node value/Convert Dictionary to JSON/Convert JSON to Dictionary)이 'JSON utilities'의
    #       11개 액션 목록에 **전부 그대로** 있다.
    "json": "JSON utilities",
    "json utilities": "JSON utilities",
}

# (문서 pkg, 문서 act) → (카탈로그 pkg, 카탈로그 act). 아래 '해석 사다리'로도 안 풀리는 잔여만 둔다.
_ACTION_ALIASES = {
    # 근거: Excel advanced의 Close 계열 액션은 카탈로그에 'Close action in Excel advanced package'
    #       단 하나다. 문서는 같은 액션을 "Close"(3회)와 "Close Spreadsheet"(1회)로 섞어 쓴다.
    #       "Close"는 해석 사다리 L2가 자동으로 푼다. "Close Spreadsheet"만 여기서 받는다.
    ("Excel advanced", "Close Spreadsheet"): ("Excel advanced", "Close action in Excel advanced package"),
    # 근거: 카탈로그 Browser 액션 8개 중 URL 파라미터를 가진 것은 'Open' 하나뿐이고
    #       (Open::['Open in','Browser tab','Browser','URL','Time out after']),
    #       문서의 "Browser > Launch website action" 바로 다음 줄이 "In the URL field, enter https://…"
    #       + 브라우저 선택이다 = 파라미터 집합이 Open과 정확히 일치한다. 'Launch website'는 구 표기.
    ("Browser", "Launch website"): ("Browser", "Open"),
    # 근거: 카탈로그 Google Drive(18개)에는 'Open spreadsheet'가 없고 Google Sheets(35개)에 있다.
    #       해당 줄(go-to-cell 튜토리얼)은 앞뒤가 전부 Google Sheets 세션(gsheet)을 쓰는 문맥이라
    #       문서 쪽 패키지 오기다. 액션명이 **정확히 한 패키지에만** 존재해 애매함이 없다.
    ("Google Drive", "Open spreadsheet"): ("Google Sheets", "Open spreadsheet"),
    # 근거: 카탈로그 'SOAP Web Service' 패키지의 액션은 'SOAP web service' **단 1개**다.
    #       문서는 복수형 "SOAP web services action"으로 쓴다.
    ("SOAP Web Service", "SOAP web services"): ("SOAP Web Service", "SOAP web service"),
}

# 카탈로그에 실재하지 않아 **자산에서 제거**하는 (문서 pkg, 문서 act).
# 별칭으로 억지로 다른 액션에 붙이면 그게 곧 환각 학습이다 — 그냥 뺀다.
# 각 항목의 근거는 REMOVED_REASONS 참고(--dry-run이 출력한다).
_REMOVED_REASONS = {
    ("CSV/TXT", "Close"): "카탈로그 CSV/TXT 액션은 ['Open','Read'] 2개뿐 — Close 없음(문서 4회 언급)",
    ("Google Drive", "Connect"): "카탈로그 Google Drive 액션 18개에 Connect 없음(OAuth 연결 액션이 코퍼스에 미적재)",
    ("Google Drive", "Disconnect"): "카탈로그 Google Drive 액션 18개에 Disconnect 없음",
    ("Google Sheets", "Connect"): "카탈로그 Google Sheets 액션 35개에 Connect 없음(Disconnect는 있음 — 비대칭 적재)",
}

# 해석 사다리에서 카탈로그 액션명을 정규화할 때 쓰는 꼬리표.
# 근거: 카탈로그에는 'Close action in Excel advanced package', 'For value action in Prompt',
#       'Loop action for data iteration', 'Authentication action in Salesforce package'처럼
#       **" action " 뒤에 설명이 붙은** 액션명이 섞여 있다. 문서는 그 앞부분("Close","For value",
#       "Loop","Authentication")만 쓴다. 그래서 " action " 이후를 잘라 비교한다.
_ACTION_TAIL = re.compile(r"\s+action\s+(?:in|for)\s+.*$", re.I)


class CatalogResolver:
    """(문서 pkg, 문서 act) → 카탈로그 (pkg, act). 전부 결정론."""

    def __init__(self) -> None:
        from app.services.catalog import get_backend_catalog

        self._cat = get_backend_catalog()
        index = self._cat._ensure_index()  # noqa: SLF001 — 오프라인 배치. 패키지 목록이 필요하다
        self.packages = sorted({p for p, _ in index})
        self._pkg_lc = {p.lower(): p for p in self.packages}
        self._acts: dict[str, list[str]] = defaultdict(list)
        for p, a in index:
            self._acts[p].append(a)
        # 사다리 L2/L3용 역인덱스 — 한 패키지 안에서 정규화 결과가 겹치면 애매하므로 후보를 모은다.
        self._norm: dict[str, dict[str, list[str]]] = {}
        for p, acts in self._acts.items():
            m: dict[str, list[str]] = defaultdict(list)
            for a in acts:
                m[_ACTION_TAIL.sub("", a).strip().lower()].append(a)
                if a.lower().startswith(p.lower() + " "):
                    m[a[len(p) + 1:].strip().lower()].append(a)
            self._norm[p] = m

    def match_package_suffix(self, text: str) -> tuple[str | None, str]:
        """`>` 왼쪽 문장에서 **카탈로그 패키지명의 최장 접미 일치**를 찾는다. → (패키지, 잔여 접두)

        왜 접미 일치인가: 문서는 "Double-click or drag the Excel advanced >"처럼 동사구를 앞에 붙인다.
        고정 접두 목록으로 지우면(실측 12종 이상) 새 문형에 취약하다. 반대로 **끝에서** 1~4토큰을
        카탈로그 패키지명과 맞춰 보면 문형과 무관하게 정확히 끊긴다. 부수 효과로 "Go to Actions >"의
        'Actions'는 카탈로그 패키지가 아니라 자동 배제된다.

        잔여 접두를 함께 돌려주는 이유: 그 꼬리가 위치 지시("… before the ")인지 봐야
        '추가'와 '참조'를 가른다(_POSITIONAL).
        """
        toks = re.split(r"\s+", text.strip())
        for n in range(4, 0, -1):
            if len(toks) < n:
                continue
            cand = " ".join(toks[-n:]).strip(" ,.")
            key = cand.lower()
            prefix = " ".join(toks[:-n]) + (" " if len(toks) > n else "")
            if key in self._pkg_lc:
                return self._pkg_lc[key], prefix
            if key in _PACKAGE_ALIASES:
                return _PACKAGE_ALIASES[key], prefix
        return None, ""

    def resolve(self, pkg: str, act: str) -> tuple[str, str] | None:
        """해석 사다리. 실패하면 None(= 자산에서 제거)."""
        pkg = _PACKAGE_ALIASES.get(pkg.lower(), self._pkg_lc.get(pkg.lower(), pkg))
        hit = _ACTION_ALIASES.get((pkg, act))
        if hit:
            return hit
        if (pkg, act) in _REMOVED_REASONS:
            return None
        acts = self._acts.get(pkg)
        if not acts:
            return None
        low = act.strip().lower()
        # L1 대소문자 무시 완전 일치 ("Excel Advanced > Set cell" 같은 표기 흔들림 흡수)
        for a in acts:
            if a.lower() == low:
                return (pkg, a)
        # L2/L3 카탈로그 액션명의 꼬리표(" action in/for …") 제거 · 패키지 접두 제거 후 일치.
        #     후보가 2개 이상이면 애매하므로 포기한다(추측 금지).
        cands = self._norm[pkg].get(low) or []
        if len(set(cands)) == 1:
            return (pkg, cands[0])
        return None

    def spec_params(self, pkg: str, act: str) -> list[str]:
        spec = self._cat.get_action_schema(pkg, act) or {}
        if spec.get("params_unknown"):
            return []
        return [str(p.get("name")) for p in (spec.get("parameters") or []) if isinstance(p, dict) and p.get("name")]


def _ground_field(field: str, spec_names: list[str]) -> str | None:
    """문서의 필드 표기를 카탈로그 스펙의 파라미터 이름으로 정규화한다. 못 맞추면 None.

    왜 정규화인가: 문서는 UI 라벨을 그대로 옮겨 "Enter data table variable"처럼 스펙 이름
    ('Data table variable') 앞에 동사를 붙이거나, "Session name"처럼 정확히 같기도 하다.
    few-shot이 존재하지 않는 필드명을 가르치면 검수 R2/R3가 잡아내지 못하는 오류를 심는다 —
    액션을 카탈로그로 검증하는 것과 같은 이유로 **파라미터 이름도 스펙에 붙여서만 싣는다.**
    """
    low = field.strip().lower()
    for n in spec_names:
        if n.lower() == low:
            return n
    # 문서 표기가 스펙 이름을 포함하는 경우 ("Enter data table variable" ⊃ "Data table variable").
    # 가장 긴 스펙 이름을 고른다 — 짧은 이름이 우연히 포함되는 오탐을 줄인다.
    contained = [n for n in spec_names if len(n) >= 5 and n.lower() in low]
    if contained:
        return max(contained, key=len)
    # 반대 방향 ("Session name" 문서 표기 ⊂ "Session name" 스펙) — 문서가 더 짧게 줄여 쓴 경우.
    containing = [n for n in spec_names if len(low) >= 5 and low in n.lower()]
    if len(set(containing)) == 1:
        return containing[0]
    return None


# ---------------------------------------------------------------------------
# 7. 문서 → 예제 레코드
# ---------------------------------------------------------------------------
# 도메인 태깅 — 소비부가 "이 업무와 비슷한 용례"를 고를 때 쓸 거친 축.
# 패키지 → 도메인 매핑만으로 정하며(추론 금지), 다수결 1위를 취한다.
_DOMAIN_BY_PACKAGE = {
    "Excel advanced": "excel", "Excel basic": "excel", "Microsoft 365 Excel": "excel",
    "Google Sheets": "spreadsheet_cloud", "Google Drive": "spreadsheet_cloud",
    "CSV/TXT": "csv", "Data Table": "datatable", "Record": "datatable",
    "Browser": "web", "Recorder": "web", "Analyze": "web",
    "Email": "email", "Gmail": "email", "Microsoft 365 Outlook": "email",
    "JSON utilities": "json", "XML": "xml",
    "Database": "database", "Snowflake": "database",
    "SAP BAPI": "sap", "SAP": "sap", "Salesforce": "crm",
    "Active Directory": "identity", "Okta": "identity",
    "SOAP Web Service": "webservice", "REST Web Services": "webservice",
    "Python Script": "script", "VBScript": "script", "JavaScript": "script", "DLL": "script",
    "Task Bot": "orchestration", "Logging": "logging",
}
# 도메인 판정에서 빼는 범용 패키지 — 어느 흐름에나 나와 변별력이 없다.
_DOMAIN_NEUTRAL = {"Loop", "If", "Error handler", "Message Box", "String", "Number",
                   "Boolean", "Dictionary", "List", "Datetime", "Delay", "Prompt", "Step"}


def _classify_kind(steps: list[dict], sections: dict[str, list[str]], title: str) -> tuple[str, str]:
    """(kind, 사유). 액션 0건 문서를 few-shot 대상에서 빼되 근거를 남긴다."""
    if steps:
        return "flow", ""
    body = " ".join(sections.get("procedure", []) + sections.get("lead", []))
    # 인덱스 페이지 — Procedure 섹션 자체가 없다(실측: 34문서 중 1건).
    if not sections.get("procedure"):
        return "excluded", "Procedure 섹션 없음 — 다른 예제로 가는 링크 목록(인덱스 페이지)"
    # 변수 문법 튜토리얼 — Actions 패널이 아니라 Variables 패널만 다룬다.
    if re.search(r"\bVariables? (?:panel|menu)\b|\bCreate variable\b|\bvariable type\b", body, re.I):
        return "variable_syntax", "액션 블록 0건 — Variables 패널의 변수 생성/구문 튜토리얼"
    # Control Room 관리 절차 — Administration 내비게이션이 본문의 뼈대다.
    # ⚠️ 'Control Room' 단독 언급은 조건에서 뺐다 — 거의 모든 튜토리얼이 한 번은 언급해 오분류한다.
    if re.search(r"\bAdministration\s*>|\bAudit log\b|\bBot update\b", body):
        return "excluded", "액션 블록 0건 — Control Room 관리(Administration) 절차, 봇 흐름 아님"
    return "excluded", "액션 블록 0건 — 개념 설명 또는 미지원 문형"


# 본문이 패키지 이름을 밝히는 관용구. `package` 외에 `session`/`function`도 받는 이유:
# 실측(remote-function-call-in-SAP)에서 그 문서는 "SAP BAPI package"라고 한 번도 안 쓰고
# "the SAP BAPI function" / "In the SAP BAPI Session field"로만 쓴다. 셋 다 **문서가 직접 쓴
# 패키지 이름**이지 우리가 추론한 게 아니다.
_PKG_MENTION = re.compile(r"([A-Z][\w ./&-]{1,28}?)\s+(?:packages?|[Ss]ession|function)\b")


def _packages_named_in_prose(lines: list[str], resolver: CatalogResolver) -> set[str]:
    """문서 본문이 이름을 **직접 밝힌** 카탈로그 패키지 집합 (G5 해석 근거)."""
    text = " ".join(lines)
    out = set()
    for m in _PKG_MENTION.finditer(text):
        pkg, _ = resolver.match_package_suffix(m.group(1))
        if pkg:
            out.add(pkg)
    return out


def _build_example(pid: str, en: list[dict], ko_title: str | None, resolver: CatalogResolver,
                   stats: Counter, removed: Counter, param_forms: Counter) -> dict:
    title = en[0]["title"]
    url = en[0]["url"] or ""
    lines = _merge_chunks(en)
    stats["lines_total"] += len(lines)
    sections = _split_sections(lines, title)

    proc = [ln for ln in sections.get("procedure", []) if not _BOILERPLATE.match(ln)]
    stats["lines_boilerplate"] += len(sections.get("procedure", [])) - len(proc)

    steps: list[dict] = []
    pending_intent = ""  # 직전의 '종결 줄' — 액션 블록의 의도 라인 후보
    open_step: dict | None = None  # 지금 파라미터를 받을 수 있는 스텝. 의도 라인이 나오면 닫힌다
    # G5(무패키지 문형)의 해석 근거 = 이 문서가 **본문에서 이름을 밝힌** 패키지.
    # 시드로 `<Pkg> package` 언급을 먼저 넣는다 — 실측(remote-function-call-in-SAP)에서 그 문서는
    # 화살표 문법을 딱 1번(맨 끝 Data table)만 쓰고 나머지는 "using the Connect action"처럼 패키지를
    # 생략하는데, 서두("SAP BAPI package")에는 패키지를 명시한다. 추측이 아니라 문서가 쓴 이름이다.
    seen_packages: set[str] = _packages_named_in_prose(lines, resolver)

    dropped: list[str] = []  # 카탈로그에 없어 뺀 액션 — 자산에 남겨 리뷰어가 손실을 볼 수 있게
    last_action_idx = -99  # 직전 액션 라인의 위치 — 인접 중복 병합 판정용
    for idx, ln in enumerate(proc):
        found = _extract_actions(ln, resolver, stats, removed, seen_packages, dropped)
        if found:
            # 문서 관용구: 의도 문장이 액션 이름을 먼저 말하고("Use the Open action to provide your
            # Visual Basic source code.") **바로 다음 줄**에 구체 문형이 온다("Double-click or drag
            # the VBScript > Open action ."). 그대로 두면 같은 스텝이 2개가 되어 few-shot이
            # "같은 액션을 연달아 두 번 넣어라"를 가르친다. 인접 + 동일 (pkg,act) + 앞 스텝에
            # 파라미터가 아직 없을 때만 병합한다 — 실제로 같은 액션을 두 번 쓰는 경우
            # (실측: reading-spreadsheet의 Go to cell 연속 2회)는 사이에 파라미터 라인이 있어 안 걸린다.
            if (steps and len(found) == 1 and idx == last_action_idx + 1
                    and (steps[-1]["package"], steps[-1]["action"]) == found[0]
                    and not steps[-1]["params"]):
                if not steps[-1]["intent"]:
                    steps[-1]["intent"] = pending_intent
                open_step = steps[-1]
                pending_intent = ""
                last_action_idx = idx
                stats["action_merged_adjacent"] += 1
                continue
            last_action_idx = idx
            for i, (pkg, act) in enumerate(found):
                steps.append({
                    "package": pkg,
                    "action": act,
                    # 의도는 블록의 첫 액션에만 붙인다 — 한 줄에 여러 액션이 나오면 뒤엣것은 종속이다.
                    "intent": pending_intent if i == 0 else "",
                    "params": [],
                    "_spec": resolver.spec_params(pkg, act),
                })
            open_step = steps[-1]
            pending_intent = ""
            continue

        parsed = _param_from_line(ln)
        if parsed:
            # ⚠️ 파라미터 문형인 줄은 **붙일 스텝이 없어도 의도 라인이 되면 안 된다.**
            #    (블록이 닫힌 뒤 나온 UI 지시가 다음 액션의 의도로 둔갑한다.)
            form, field, value = parsed
            param_forms[form] += 1
            stats["param_lines_matched"] += 1
            if open_step is None:
                stats["param_orphan"] += 1
                continue
            grounded = _ground_field(field, open_step["_spec"])
            if grounded:
                # 같은 필드가 두 번 나오면(문서가 같은 값을 재확인) 뒤엣것을 버린다.
                if grounded not in [p[0] for p in open_step["params"]]:
                    open_step["params"].append([grounded, value])
                stats["param_grounded"] += 1
            else:
                stats["param_ungrounded"] += 1
            continue

        # 의도 라인 — 액션도 파라미터도 아니고, 문장으로 끝나며(마침표/콜론), 너무 길지 않은 줄.
        #
        # ⚠️ 여기서 **열린 액션 블록을 닫는다**. 안 닫으면 문서가 미지원 문형으로 액션을 추가한
        #    구간의 파라미터가 훨씬 앞의 엉뚱한 스텝에 붙는다(실측: SAP 문서에서 'BAPI function
        #    alias'가 `Data Table > Write to file`에 7회 붙었다). 문서 구조가 실제로
        #    [의도 문장] → [액션 라인] → [파라미터 라인들] 이라 이 규칙이 곧 블록 경계다.
        if 12 <= len(ln) <= 200 and ln.rstrip().endswith((".", ":")):
            open_step = None
            if not _NOT_INTENT.match(ln):
                pending_intent = ln.rstrip(" :")

    for s in steps:
        s.pop("_spec", None)

    kind, kind_reason = _classify_kind(steps, sections, title)
    packages = sorted({s["package"] for s in steps})
    # 도메인은 **스텝 수로 가중**한다 — 패키지 집합만 세면 곁다리 1스텝(예: SAP 흐름 끝의
    # Data Table > Write to file)이 주 도메인과 1:1 동률이 돼 정렬 순서로 승자가 갈린다(실측:
    # SAP 문서가 'datatable'로 찍혔다).
    domain_votes: Counter = Counter()
    for s in steps:
        p = s["package"]
        if p in _DOMAIN_NEUTRAL:
            continue
        d = _DOMAIN_BY_PACKAGE.get(p)
        if d:
            domain_votes[d] += 1
    domain = domain_votes.most_common(1)[0][0] if domain_votes else ("control_flow" if packages else "")

    # 업무 1문장 요약: lead(제목 바로 아래 요약문) 첫 문장 → 없으면 About this task 첫 문장.
    summary = ""
    for key in ("lead", "about"):
        for ln in sections.get(key, []):
            if len(ln) >= 25 and not _BOILERPLATE.match(ln):
                summary = ln
                break
        if summary:
            break

    return {
        "doc_id": pid,
        "title": title,
        "title_ko": ko_title or "",
        "url": url,
        "kind": kind,
        "kind_reason": kind_reason,
        "domain": domain,
        "task_summary": summary,
        "packages": packages,
        "steps": steps,
        # steps는 전량 카탈로그 실재 확인됨(제거 정책) — 항상 True다. 대신 무엇을 잃었는지를
        # dropped_actions로 남긴다. 이게 없으면 "해석률 100%"가 손실을 감춘 값처럼 읽힌다.
        "catalog_resolved": True,
        "dropped_actions": sorted(set(dropped)),
        "holdout": False,
        "holdout_reason": "",
        "related_goldset": [],
        "_body": " ".join(proc),  # 홀드아웃 감사용. 산출 직전에 뺀다
    }


def _extract_actions(line: str, resolver: CatalogResolver, stats: Counter,
                     removed: Counter, seen_packages: set[str],
                     dropped: list[str] | None = None) -> list[tuple[str, str]]:
    """한 줄에서 (package, action)들을 뽑아 카탈로그로 정규화한다. 미해석은 버린다.

    `seen_packages`는 **같은 문서에서 이미 등장한 패키지** — G5(무패키지 문형)의 해석 근거다.
    문법은 G1 → G2 → G3 → G4 → G5 순으로 시도하고, 앞 문법이 잡으면 뒤는 보지 않는다
    (같은 액션을 두 문법이 중복으로 잡아 스텝이 부풀지 않게).
    """
    raw: list[tuple[str, str]] = []

    for m in _ARROW.finditer(line):
        if _in_parenthetical(line[: m.start()]):
            continue
        pkg, prefix = resolver.match_package_suffix(m.group(1))
        if pkg and not _POSITIONAL.search(prefix):
            raw.append((pkg, m.group(2).strip()))
    if not raw:
        for m in _G2.finditer(line):
            pkg, _ = resolver.match_package_suffix(m.group(1))
            if pkg:
                raw.append((pkg, m.group(2).strip()))
    if not raw:
        for m in _G3.finditer(line):
            pkg, _ = resolver.match_package_suffix(m.group(1))
            if pkg:
                raw.append((pkg, m.group(2).strip()))
    if not raw:
        for m in _G4.finditer(line):
            prefix = line[: m.start()]
            if _POSITIONAL.search(prefix) or _in_parenthetical(prefix):
                continue
            hit = _BARE_ACTION_PKG.get(re.sub(r"\s+", " ", m.group(1).lower()))
            if hit:
                raw.append(hit)
    if not raw:
        for m in _G5.finditer(line):
            prefix = line[: m.start()]
            if _POSITIONAL.search(prefix) or _in_parenthetical(prefix):
                continue
            act = m.group(1).strip()
            cands = {resolver.resolve(p, act) for p in seen_packages}
            cands.discard(None)
            if len(cands) == 1:  # 후보가 2개 이상이면 애매 — 포기(추측 금지)
                raw.append(next(iter(cands)))

    out: list[tuple[str, str]] = []
    for pkg, act in raw:
        stats["action_blocks_raw"] += 1
        hit = resolver.resolve(pkg, act)
        if hit is None:
            removed[(pkg, act)] += 1
            stats["action_removed"] += 1
            if dropped is not None:
                dropped.append(f"{pkg} > {act}")
            continue
        out.append(hit)
        stats["action_resolved"] += 1
        seen_packages.add(hit[0])
    return out


# ---------------------------------------------------------------------------
# 8. 홀드아웃 / 골드셋 중첩
# ---------------------------------------------------------------------------
# 기본 홀드아웃 1건 — 골드셋 케이스 13(Invoicely)과 **해법 골격**이 사실상 같다
# (스프레드시트 행 루프 → 웹폼 입력 → 제출). 어휘 유출은 없어도 패턴 유출이다.
_DEFAULT_HOLDOUT_URL_SLUG = "enter-data-into-webform-from-file"
_DEFAULT_HOLDOUT_REASON = (
    "골드셋 13(Invoicely)과 해법 골격 동일(스프레드시트 행 루프 → 웹폼 입력 → 제출) — 패턴 유출"
)

_WORD = re.compile(r"[a-z0-9]+")


def _ngrams(text: str, n: int = 4) -> set[tuple]:
    w = _WORD.findall(text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _load_goldset() -> dict[str, str]:
    if not GOLDSET_DIR.exists():
        return {}
    out = {}
    for p in sorted(GOLDSET_DIR.glob("*.md")):
        out[p.name[:2]] = p.read_text(encoding="utf-8", errors="replace")
    return out


def _goldset_overlap(body: str, goldset: dict[str, str]) -> list[tuple[str, int]]:
    """예제 본문과 각 골드셋 문서의 영문 4-gram 교집합 크기. 큰 순.

    ⚠️ 실측 결과 **전 조합 0**이다. 골드셋 업무정의서는 100% 한국어 산문이고 자산은 영문 문서라
    어휘가 겹칠 수 없다. 즉 이 지표는 '어휘 유출 없음'을 확인해 줄 뿐 **패턴 유출은 못 잡는다** —
    그래서 아래 `_domain_overlap`(도메인 골격 중첩)을 함께 돌리고, related_goldset은 그쪽으로 채운다.
    """
    bg = _ngrams(body)
    scored = [(cid, len(bg & _ngrams(txt))) for cid, txt in goldset.items()]
    return sorted(scored, key=lambda x: -x[1])


# 골드셋 업무정의서의 "사용 프로그램 및 시스템:" 항목 → 도메인 태그.
# 실측으로 13건의 해당 줄을 전수 조사해 나온 어휘만 넣었다(추정 어휘 금지).
_GOLDSET_DOMAIN_KEYWORDS = {
    "excel": ["Excel", "엑셀"],
    "email": ["Outlook", "메일", "SMTP", "이메일"],
    "web": ["Chrome", "사이트", "웹"],
    "csv": ["CSV"],
    "pdf": ["PDF"],
    "word": ["Word"],
    "script": ["DLL", ".NET"],
    "webservice": ["REST API", "API"],
    "xml": ["XML"],
}
# 자산 쪽 도메인을 골드셋 태그와 같은 축으로 옮긴다(패키지→도메인 표가 더 세분돼 있어서).
_ASSET_DOMAIN_TO_GOLDSET = {
    "excel": "excel", "spreadsheet_cloud": "excel", "csv": "csv", "datatable": "csv",
    "web": "web", "email": "email", "script": "script", "webservice": "webservice",
    "xml": "xml", "json": "webservice",
}
# 도메인 중첩 경보 임계. 2 = 서로 다른 축 2개가 겹친다(예: 웹+엑셀) → 해법 골격이 닮았다는 신호.
_OVERLAP_ALERT = 2


def _goldset_domains(goldset: dict[str, str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cid, txt in goldset.items():
        systems = " ".join(re.findall(r"사용 프로그램 및 시스템\s*:\s*(.+)", txt))
        title = " ".join(re.findall(r"과제명\s*:\s*(.+)", txt))
        blob = systems + " " + title
        out[cid] = {d for d, kws in _GOLDSET_DOMAIN_KEYWORDS.items() if any(k in blob for k in kws)}
    return out


def _example_domains(ex: dict) -> set[str]:
    """예제가 건드리는 도메인 축 전부 (대표 domain 1개가 아니라 패키지 전량 기준)."""
    out = set()
    for p in ex["packages"]:
        d = _DOMAIN_BY_PACKAGE.get(p)
        if d and d in _ASSET_DOMAIN_TO_GOLDSET:
            out.add(_ASSET_DOMAIN_TO_GOLDSET[d])
    return out


def _domain_overlap(ex: dict, gdom: dict[str, set[str]]) -> list[tuple[str, int, set[str]]]:
    """예제 ↔ 각 골드셋 케이스의 도메인 축 교집합. 큰 순."""
    ed = _example_domains(ex)
    scored = [(cid, len(ed & d), ed & d) for cid, d in gdom.items()]
    return sorted(scored, key=lambda x: (-x[1], x[0]))


# ---------------------------------------------------------------------------
# 9. 파이프라인
# ---------------------------------------------------------------------------
def build() -> tuple[dict, Counter, Counter, Counter]:
    rows = _fetch_docs()
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["locale"], r["pid"])].append(r)

    # EN/KO 짝짓기는 URL 슬러그로 한다 — 제목은 번역돼 있어 못 쓰고, id/parent_id는 로케일마다 다르다.
    # 실측: 슬러그 34개가 EN/KO 양쪽에서 **정확히 1:1**로 맞는다(교집합 34, 한쪽만 있는 건 0).
    def slug(u: str | None) -> str:
        return (u or "").rstrip("/").rsplit("/", 1)[-1]

    # KO 제목은 "<영문 제목> / <한국어 제목>"로 저장돼 있다(실측 34/34). 영문 부분은 title과 중복이라 뗀다.
    def ko_only(t: str) -> str:
        return t.split(" / ", 1)[1].strip() if " / " in t else t

    ko_title_by_slug = {slug(ch[0]["url"]): ko_only(ch[0]["title"])
                        for (loc, _), ch in by.items() if loc == "ko-KR"}

    resolver = CatalogResolver()
    stats, removed, param_forms = Counter(), Counter(), Counter()
    examples = []
    for (loc, pid), ch in sorted(by.items(), key=lambda kv: kv[1][0]["title"]):
        if loc != "en-US":
            continue
        stats["docs"] += 1
        examples.append(_build_example(pid, ch, ko_title_by_slug.get(slug(ch[0]["url"])),
                                       resolver, stats, removed, param_forms))

    goldset = _load_goldset()
    gdom = _goldset_domains(goldset)
    for ex in examples:
        if slug(ex["url"]) == _DEFAULT_HOLDOUT_URL_SLUG:
            ex["holdout"] = True
            ex["holdout_reason"] = _DEFAULT_HOLDOUT_REASON
        if goldset and ex["kind"] == "flow":
            ex["related_goldset"] = [cid for cid, n, _ in _domain_overlap(ex, gdom) if n >= _OVERLAP_ALERT]

    asset = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_LABEL,
        "generated_from": {"locale": "en-US", "doc_count": stats["docs"]},
        "examples": examples,
    }
    return asset, stats, removed, param_forms


def _strip_internal(asset: dict) -> dict:
    out = json.loads(json.dumps(asset, ensure_ascii=False))
    for ex in out["examples"]:
        ex.pop("_body", None)
    return out


# ---------------------------------------------------------------------------
# 10. CLI
# ---------------------------------------------------------------------------
def _cmd_dry_run(asset, stats, removed, param_forms) -> int:
    ex = asset["examples"]
    kinds = Counter(e["kind"] for e in ex)
    print("== 추출 통계 ==")
    print(f"  문서(en-US)          : {stats['docs']}")
    print(f"  병합 줄 수           : {stats['lines_total']}")
    print(f"  보일러플레이트 제거   : {stats['lines_boilerplate']}줄")
    for k, v in kinds.most_common():
        print(f"  kind={k:<16}: {v}건")
    print(f"  액션 블록(원시)      : {stats['action_blocks_raw']}")
    print(f"  액션 해석 성공       : {stats['action_resolved']}")
    print(f"  액션 제거(카탈로그 무): {stats['action_removed']}")
    rate = stats["action_resolved"] / max(1, stats["action_blocks_raw"])
    print(f"  카탈로그 해석률      : {rate:.3f}")
    print(f"  액션 인접중복 병합    : {stats['action_merged_adjacent']}")
    print(f"  파라미터 라인(문형매칭): {stats['param_lines_matched']}")
    print(f"    ├ 스펙 접지 성공    : {stats['param_grounded']}")
    print(f"    ├ 접지 실패(폐기)   : {stats['param_ungrounded']}  (스펙 flat 목록에 없는 조건부 하위 필드가 대부분)")
    print(f"    └ 붙일 스텝 없음     : {stats['param_orphan']}  (미지원 문형으로 추가된 액션의 뒤따름)")
    print("\n== 파라미터 문형 분포 ==")
    for form, n in param_forms.most_common():
        print(f"  {n:>4}  {form}")
    print("\n== 카탈로그 미해석으로 제거한 액션 ==")
    for (p, a), n in removed.most_common():
        why = _REMOVED_REASONS.get((p, a), "(사다리·별칭 모두 실패)")
        print(f"  {n:>3}회  {p} > {a}\n         근거: {why}")
    flows = [e for e in ex if e["kind"] == "flow"]
    print(f"\n== flow 예제 {len(flows)}건 (steps 수) ==")
    for e in sorted(flows, key=lambda x: -len(x["steps"])):
        print(f"  {len(e['steps']):>2} steps | {len(sum([s['params'] for s in e['steps']], [])):>2} params"
              f" | {e['domain']:<18} | {e['title'][:62]}")
    for e in ex:
        if e["kind"] != "flow":
            print(f"   -- {e['kind']:<16} | {e['title'][:62]} | {e['kind_reason']}")
    return 0


def _cmd_verify_catalog(asset) -> int:
    resolver = CatalogResolver()
    bad = []
    total = 0
    for e in asset["examples"]:
        for s in e["steps"]:
            total += 1
            if resolver._cat.get_action_schema(s["package"], s["action"]) is None:  # noqa: SLF001
                bad.append((e["title"], s["package"], s["action"]))
    print(f"자산 내 (pkg, act) 총 {total}건 / 카탈로그 미실재 {len(bad)}건")
    for t, p, a in bad:
        print(f"  ✗ {p} > {a}   ({t})")
    if bad:
        print("FAIL — 미해석 0건이어야 한다")
        return 1
    print("OK — 전 항목 카탈로그 실재 확인")
    return 0


def _cmd_holdout_audit(asset) -> int:
    goldset = _load_goldset()
    if not goldset:
        print(f"골드셋 디렉터리 없음: {GOLDSET_DIR}")
        return 1
    gdom = _goldset_domains(goldset)
    print(f"골드셋 {len(goldset)}건 도메인 태그: " +
          ", ".join(f"{c}={sorted(d) or ['-']}" for c, d in sorted(gdom.items())))

    ng_max = max((_goldset_overlap(e.get("_body", ""), goldset)[0][1]
                  for e in asset["examples"] if e["kind"] == "flow"), default=0)
    print(f"\n[1] 어휘 유출 — 영문 4-gram 교집합 최대치: {ng_max}")
    print("    (골드셋은 100% 한국어 산문, 자산은 영문 → 구조적으로 0. 어휘 유출 없음은 확인되나"
          " 패턴 유출은 이 지표로 못 잡는다.)")

    print(f"\n[2] 패턴 유출 — 도메인 축 교집합 (임계 {_OVERLAP_ALERT})")
    rows = []
    for e in asset["examples"]:
        if e["kind"] != "flow":
            continue
        top = _domain_overlap(e, gdom)
        rows.append((top[0][1], e, top))
    for best, e, top in sorted(rows, key=lambda r: -r[0]):
        mark = "HOLDOUT" if e["holdout"] else ("  경보 " if best >= _OVERLAP_ALERT else "       ")
        hits = " ".join(f"{c}({'+'.join(sorted(d))})" for c, n, d in top if n >= _OVERLAP_ALERT)
        print(f"{mark} 축{best}  {e['title'][:56]:<56} {sorted(_example_domains(e))} → {hits}")
    print("\n현재 홀드아웃:")
    for e in asset["examples"]:
        if e["holdout"]:
            print(f"  · {e['title']}\n    사유: {e['holdout_reason']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="자동화 용례 34건 → few-shot 자산 JSON")
    ap.add_argument("--dry-run", action="store_true", help="추출 통계만 출력(파일 미기록)")
    ap.add_argument("--verify-catalog", action="store_true", help="자산의 모든 (pkg, act) 실재 검증")
    ap.add_argument("--holdout-audit", action="store_true", help="골드셋과의 4-gram 교집합 측정")
    ap.add_argument("--out", default=str(OUT_PATH), help="산출 경로")
    args = ap.parse_args(argv)

    asset, stats, removed, param_forms = build()

    if args.dry_run:
        return _cmd_dry_run(asset, stats, removed, param_forms)
    if args.verify_catalog:
        return _cmd_verify_catalog(asset)
    if args.holdout_audit:
        return _cmd_holdout_audit(asset)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_strip_internal(asset), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n_flow = sum(1 for e in asset["examples"] if e["kind"] == "flow")
    n_step = sum(len(e["steps"]) for e in asset["examples"])
    print(f"기록: {out}  (예제 {len(asset['examples'])}건 / flow {n_flow}건 / 스텝 {n_step}개)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
