"""사용자 제공 카탈로그의 규칙 파싱·교차 검증 (RPA-285).

규칙 파서의 존재 이유는 규모다. LLM 재출력은 액션당 약 90토큰이 들어 448개면 출력 4만
토큰이 필요한데, 실측에서 모델이 796토큰에 포기하고 **0개**를 반환했다. 규칙으로 읽으면
같은 카탈로그가 LLM 0콜로 통과한다.

그래서 이 파일이 지키는 것은 두 가지다:
  ① 규칙이 실제 형식들을 읽는가            (못 읽으면 LLM 폴백이라 손해는 비용뿐)
  ② 규칙이 **엉뚱한 걸 읽지 않는가**        (이쪽이 위험하다 — 조용히 틀린 어휘가 확정된다)
"""

from app.agent.v3.orchestrator.catalog_parse import parse_catalog
from app.agent.v3.orchestrator.generate import _agrees_with_signal

SLIM = """# Power Automate for desktop 액션 카탈로그

## Browser automation
- **Launch new Microsoft Edge** — Edge를 실행한다. / 필수: Initial URL(Text value)*, Timeout(Numeric value)*
- **Go to web page** — 페이지로 이동한다. / 필수: URL(Text value)*

## Excel
- **Launch Excel** — 엑셀을 연다. / 필수: Document path(File)*
- **Close Excel** — 엑셀을 닫는다.
"""

PAREN = """## Excel
- Launch Excel (Document path, Sheet name)
- Write to Excel worksheet (Excel instance, Value to write, Column, Row)
- Close Excel (Excel instance)
"""

TABLE = """### Excel (`excel`) — 액션 3개

| 액션 | 설명 | 입력 파라미터 | 생성 변수 |
|---|---|---|---|
| **열기** (`OpenSpreadsheet`) | 연다 | 파일 경로(FILE*), 세션 이름(TEXT*) | - |
| **닫기** (`CloseSpreadsheet`) | 닫는다 | 세션 이름(TEXT*) | - |
| **읽기** (`ReadCell`) | 읽는다 | 셀(TEXT*) | STRING |
"""

PAIRS = """우리는 UiPath를 씁니다.
- UiPath.Excel.Activities/ReadRange
- UiPath.Excel.Activities/WriteRange
- UiPath.Mail.Activities/SendOutlookMail
"""


# ─────────────────────────────────────────────────────────────────────────────
# ① 읽는다
# ─────────────────────────────────────────────────────────────────────────────

def test_parses_bullet_catalog_with_required_params():
    got = parse_catalog(SLIM)
    assert [a["action"] for a in got] == [
        "Launch new Microsoft Edge", "Go to web page", "Launch Excel", "Close Excel"]
    assert got[0]["package"] == "Browser automation"
    assert got[0]["parameters"][0] == {"name": "Initial URL", "required": True, "type": "Text value"}


def test_parses_parenthesised_params():
    got = parse_catalog(PAREN)
    write = next(a for a in got if a["action"] == "Write to Excel worksheet")
    assert [p["name"] for p in write["parameters"]] == [
        "Excel instance", "Value to write", "Column", "Row"]


def test_parses_markdown_table_with_internal_ids():
    got = parse_catalog(TABLE)
    assert [a["action"] for a in got] == ["OpenSpreadsheet", "CloseSpreadsheet", "ReadCell"]
    assert got[0]["label"] == "열기"  # 표기 보존 — 라벨은 사람이 고르는 근거다
    assert got[0]["package"] == "Excel"


def test_parses_package_slash_action_pairs():
    got = parse_catalog(PAIRS)
    assert ("UiPath.Excel.Activities", "ReadRange") in {(a["package"], a["action"]) for a in got}


def test_parses_json_catalog():
    got = parse_catalog('[{"package":"Excel","action":"Open"},'
                        ' {"package":"Excel","action":"Close"},'
                        ' {"package":"Web","action":"Goto"}]')
    assert len(got) == 3


# ─────────────────────────────────────────────────────────────────────────────
# ② 엉뚱한 걸 읽지 않는다 (이쪽이 위험하다)
# ─────────────────────────────────────────────────────────────────────────────

def test_unknown_parameters_omit_the_key_not_empty_list():
    """모름과 없음을 가른다 — `[]`면 체커가 R2로 "스펙에 없는 파라미터"를 잡는다."""
    got = parse_catalog(SLIM)
    close = next(a for a in got if a["action"] == "Close Excel")
    assert "parameters" not in close, "파라미터를 못 읽었으면 키 자체가 없어야 한다"


def test_overview_and_group_tables_are_not_actions():
    """액션 표가 아닌 표(개요·그룹 목록)를 액션으로 읽으면 카탈로그가 부풀어 오른다."""
    doc = """## 개요
| 항목 | 건수 |
|---|---|
| 액션 | 3 |

## 그룹 목록
| 그룹 | 표시 이름 | 액션 수 |
|---|---|---|
| excel | Excel | 3 |

""" + TABLE
    got = parse_catalog(doc)
    assert len(got) == 3, f"액션 표만 읽어야 한다 (읽은 것: {[a['action'] for a in got]})"


def test_prose_and_empty_input_are_rejected():
    assert parse_catalog("매일 아침 9시에 메일 보내는 봇 만들어줘") is None
    assert parse_catalog("") is None
    assert parse_catalog(None) is None


def test_too_few_items_is_rejected():
    assert parse_catalog("## G\n- Only one thing\n") is None


def test_code_fences_are_skipped():
    doc = "## Excel\n```\n- Fake/Action1\n- Fake/Action2\n- Fake/Action3\n```\n" + PAREN
    got = parse_catalog(doc)
    assert not any(a["package"] == "Fake" for a in got)


# ─────────────────────────────────────────────────────────────────────────────
# 교차 검증 — LLM 판정과 어긋나면 규칙을 버린다
# ─────────────────────────────────────────────────────────────────────────────

def _parsed():
    return parse_catalog(SLIM)


def test_signal_agreement_accepts_matching_samples():
    state = {"catalog_signal": {"sample_actions": ["Excel/Launch Excel", "Browser automation/Go to web page"]}}
    assert _agrees_with_signal(_parsed(), state)


def test_signal_agreement_matches_on_action_name_alone():
    """패키지 표기는 LLM과 규칙이 갈릴 수 있다 — 액션 이름만으로도 일치로 본다."""
    state = {"catalog_signal": {"sample_actions": ["아무거나/Launch Excel", "Close Excel"]}}
    assert _agrees_with_signal(_parsed(), state)


def test_signal_agreement_rejects_when_samples_are_elsewhere():
    """LLM이 본 카탈로그와 규칙이 읽은 목록이 다르면 규칙을 버리고 LLM으로 넘긴다."""
    state = {"catalog_signal": {"sample_actions": [
        "UiPath.Excel.Activities/ReadRange", "UiPath.Mail.Activities/SendOutlookMail"]}}
    assert not _agrees_with_signal(_parsed(), state)


def test_signal_agreement_passes_when_no_samples():
    """대조할 근거가 없으면 막지 않는다 — 없는 근거로 막으면 빠른 길이 영영 닫힌다."""
    assert _agrees_with_signal(_parsed(), {})
    assert _agrees_with_signal(_parsed(), {"catalog_signal": {"present": True}})
