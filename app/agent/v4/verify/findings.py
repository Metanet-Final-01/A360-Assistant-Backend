"""검증 계층 공통 산출 포맷 Finding — L0/L1(정적)·L2(시맨틱)·L3(시뮬레이션)의 정규화.

심판(judge)과 refine 루프가 계층 구분 없이 소비하는 단일 어휘다. severity 순서가
refine의 처리 우선순위이자 수렴 판정(가중합 단조 감소)의 축이 된다.
"""

from pydantic import BaseModel, Field

# 심각도 가중치 — refine 회귀 가드의 '위반이 줄었는가' 판정에 쓴다.
SEVERITY_WEIGHT = {"blocker": 100, "major": 10, "minor": 3, "warning": 1}


class Finding(BaseModel):
    """검증 계층 공통 발견 사항 한 건."""

    layer: str = Field(description="L0|L1|L2|L3|judge")
    severity: str = Field("major", description="blocker|major|minor|warning")
    rule: str | None = Field(None, description="R1~R12 (정적 계층일 때)")
    req_id: str | None = Field(None, description="FlowSpec 요구 id (시맨틱 계층일 때)")
    location: str | None = Field(None, description="트리 경로 또는 node_id")
    step_id: str | None = None
    message: str = ""
    fix_hint: str | None = Field(None, description="surgeon에게 줄 수리 힌트")


def weight(findings: list[Finding]) -> int:
    """심각도 가중합 — 낮을수록 좋다. refine 라운드 회귀 가드의 비교값."""
    return sum(SEVERITY_WEIGHT.get(f.severity, 10) for f in findings)


# R3는 질문 카드로 승격되므로 결함 축에서 분리한다 (설계 관찰 3 — 정보 부족 ≠ 결함).
# 다만 **전부는 아니다** — 아래 _r3_is_defect가 파라미터 타입으로 갈라낸다.
_CARD_RULES = {"R3"}

# ─────────────────────────────────────────────────────────────────────────────
# R3 분류 — 동작 옵션(결함) vs 업무 데이터(질문 카드)  (설계 §5.2-E)
# ─────────────────────────────────────────────────────────────────────────────
# 왜 가르는가: 우리 사용자는 RPA 비전문가다(과제 요구사항 — "전문 지식 없이도", "별도
# 교육 없이"). 필수 파라미터 미충족을 전부 카드로 보내면 "Read-write 모드인가요?",
# "세션 이름을 정해 주세요" 같은 **답할 수 없는 질문**이 사용자에게 간다. 파라미터는
# 성격이 둘로 갈리고, 카탈로그의 `type`이 그 경계를 결정론으로 그어 준다.
#
# 로컬 카탈로그 실측(2026-07-25, action_schema 1,375행 / 파라미터 7,648개):
#
#     TYPE          전체   required        성격
#     TEXT          2155    620    자유 입력 — 셀 주소·검색어         → 카드
#     SELECT        1385    451    선택지 고정 — 모드·범위            → 결함
#     SESSION        952    507    흐름도 내부 핸들 이름              → 결함
#     NUMBER         801     59    자유 입력 — 행 번호·반복 횟수      → 카드
#     BOOLEAN        746      1    2지선다 토글 — 헤더 유무 등        → 결함
#     VARIABLE       692    240    산출 변수 이름(흐름도 내부)        → 결함
#     FILE           274    142    파일·폴더 경로                     → 카드
#     LIST           246     42    자유 입력                          → 카드
#     CREDENTIAL     210     34    계정·토큰 — 사용자만 안다          → 카드
#     DICTIONARY     109     23    자유 입력                          → 카드
#     UNKNOWN         78      9    타입 미상 — 모름 → 카드            → 카드
#
# 실측에서 얻은 두 가지:
#   ① RADIO는 현행 카탈로그에 **0건**이다(구세대 JAR 표기). 그래도 목록에 남긴다 —
#      재적재로 표기가 되살아나면 자동으로 옳게 동작한다. 반대로 CHECKBOX/TOGGLE 같은
#      추측 타입은 **넣지 않는다**(0건이고 근거도 없다).
#   ② `options`가 채워진 파라미터가 **0건**이다 → R4(enum 값 검사)도 사실상 미발화다.
#      그래서 판별을 options 유무가 아니라 **type**에 건다. options에 걸면 전량 카드로
#      떨어져 이 설계가 통째로 죽는다.
_OPTION_PARAM_TYPES = frozenset({"RADIO", "SELECT", "BOOLEAN"})

# 흐름도 **내부 식별자** — 세션 핸들·산출 변수 이름. 타입이 선택지 고정형은 아니지만
# 카드로 보내면 안 되는 건 같은 이유다: 사용자가 알 수 없고 답할 수도 없다("사용자만
# 안다 → 질문 카드"의 정확한 반대). required 실측 747건(SESSION 507 + VARIABLE 240)이
# 전부 카드로 나가면 비전문가에게 내부 명명을 떠넘기는 꼴이 된다. 게다가 세션 이름이
# 비면 R7/R8 추적이 어긋나므로 **에이전트가 채우는 편이 검수 정합에도 맞다.**
_INTERNAL_PARAM_TYPES = frozenset({"SESSION", "VARIABLE"})


def _param_type(d: dict) -> str:
    """위반이 실어 온 카탈로그 파라미터 타입 (대문자, 없으면 "").

    두 표기를 다 읽는다: `Violation.as_dict()`가 승격해 주는 `param_type`(실사용 경로 —
    harness의 dict 셔틀을 지난다)과 원본 `spec_excerpt["type"]`(Violation을 직접 넘기는
    경로·테스트). 둘 중 하나만 보면 한쪽 경로가 조용히 '타입 미상'이 된다.
    """
    value = d.get("param_type")
    if value is None:
        value = (d.get("spec_excerpt") or {}).get("type")
    return str(value or "").strip().upper()


def _r3_is_defect(d: dict) -> bool:
    """이 R3가 '동작 옵션/내부 식별자 미확정'(결함)인가 — 아니면 업무 데이터(카드)인가.

    타입을 모르면 **카드 쪽으로 떨어진다.** 타 솔루션 카탈로그(대화 추출)는 파라미터
    타입이 없는 경우가 많고(UiPath·Blue Prism은 enum 자체가 희소), 근거 없이 결함으로
    올리면 교정 루프가 아무 값이나 채우게 된다. '모름 → 침묵' 원칙의 R3판이다.
    """
    ptype = _param_type(d)
    return ptype in _OPTION_PARAM_TYPES or ptype in _INTERNAL_PARAM_TYPES


def _r3_fix_hint(d: dict) -> str:
    """surgeon에게 줄 수리 지시 — '무엇을 물어보라'가 아니라 '무엇을 확정하라'."""
    param = d.get("param") or "이 파라미터"
    if _param_type(d) in _INTERNAL_PARAM_TYPES:
        return (
            f"'{param}'은(는) 흐름도 내부 식별자(세션 핸들·산출 변수)입니다 — 사용자에게 "
            "묻지 말고 흐름도 안에서 일관된 이름을 정해 채우세요."
        )
    return (
        f"'{param}'은(는) 선택지가 카탈로그에 정해진 동작 옵션입니다 — 사용자에게 묻지 말고 "
        "액션 문서 근거로 값을 확정하세요. 근거가 없으면 문서의 기본값을 씁니다."
    )


# 규칙별 기본 심각도 — R1(환각)은 blocker, 구조·세션은 major, 스타일·경고는 warning.
# R13/R14(제어 흐름 구조)는 실행 의미가 깨지는 결함이라 major (warning 변형은
# Violation.severity가 덮는다 — 빈 Try/Loop 본문 등).
_RULE_SEVERITY = {
    "R1": "blocker",
    "R2": "major", "R4": "major", "R5": "minor", "R6": "major",
    "R7": "major", "R8": "major", "R9": "major", "R11": "major",
    "R13": "major", "R14": "major",
    "R10": "warning", "R12": "warning",
    # R15(attended 함정)·R16(플랫폼)은 환경 가정에 의존하는 경고 — 수리를 강제하지 않는다.
    "R15": "warning", "R16": "warning",
    # R17(세션 핸들 패키지 불일치)은 **실행이 확실히 멈추는** 결함이라 R1(환각)과 동급이다
    # (RPA-298). 실측: Excel advanced로 연 워크북을 Microsoft 365 Excel이 받아 3.5에서 실패.
    # major로 두면 교정 루프가 다른 위반과 저울질하다 그냥 남길 수 있다 — 여기선 안 된다.
    "R17": "blocker",
    # R18(비실행 구획이 요구 담당)은 액션 하나를 채우면 풀리는 결함이라 major.
    "R18": "major",
}


# 규칙별 수리 힌트 — surgeon 프롬프트의 [고칠 문제들] 줄에 붙는다.
#
# 실측(2026-07-27): R18 5건이 4라운드에 걸쳐 **한 번도** 수리되지 않았다. surgeon은 매 라운드
# 시도했지만 `update`에 package만 주고 action_name을 빼서, 결과 표기가 `Microsoft 365 Excel/Step`
# ·`Recorder/Step`·`Browser/Step`·`Email/Step`이 됐다 — 전부 없는 액션이라 사전 검증이 버렸다
# (라운드별 5·2·5·4건, 총 16건). 규칙 설명만으로는 그 함정이 안 보인다: 모델은 "패키지를
# 바꿨다"고 생각하지 자기가 없는 표기를 **만들고 있다**고 인식하지 못한다.
_FIX_HINT: dict[str, str] = {
    "R18": (
        "`update`로 package와 action_name을 **둘 다** 지정하세요. package만 바꾸면 옛 액션 "
        "이름이 그대로 남아 `Microsoft 365 Excel/Step` 같은 없는 표기가 되어 통째로 무시됩니다"
    ),
    "R17": (
        "핸들을 연 패키지에서 같은 일을 하는 액션을 골라 `update` — 여기서도 package와 "
        "action_name을 둘 다 주세요"
    ),
}


def from_violations(violations: list) -> tuple[list[Finding], list]:
    """checker Violation 목록 → (Finding 목록, 카드 후보 R3 위반 목록).

    Violation의 severity="warning"(Loop 누수 등)은 규칙 기본값보다 우선한다.

    R3는 파라미터 타입으로 갈린다 — 동작 옵션·내부 식별자는 major Finding(우리가 확정할
    책임), 업무 데이터만 카드 후보다(설계 §5.2-E, 판별은 `_r3_is_defect`).
    """
    findings: list[Finding] = []
    card_candidates: list = []
    for v in violations:
        d = v.as_dict() if hasattr(v, "as_dict") else dict(v)
        rule = d.get("rule")
        if rule in _CARD_RULES:
            if not _r3_is_defect(d):
                card_candidates.append(v)
                continue
            findings.append(
                Finding(
                    layer="L0", severity="major", rule=rule,
                    location=d.get("location"),
                    step_id=d.get("step_id"),
                    message=d.get("message", ""),
                    fix_hint=_r3_fix_hint(d),
                )
            )
            continue
        severity = _RULE_SEVERITY.get(rule, "major")
        if d.get("severity") == "warning":
            severity = "warning"
        findings.append(
            Finding(
                layer="L0" if rule in ("R1", "R2", "R4", "R5", "R6") else "L1",
                severity=severity,
                rule=rule,
                location=d.get("location"),
                step_id=d.get("step_id"),
                message=d.get("message", ""),
                fix_hint=_FIX_HINT.get(rule),
            )
        )
    return findings, card_candidates


def from_coverage(report) -> list[Finding]:
    """L2 CoverageReport → Finding 목록. must 미충족은 blocker(심판 하드 게이트 원료)."""
    findings: list[Finding] = []
    for e in report.entries:
        if e.status == "covered":
            continue
        if e.status == "unknown":  # 정보 부족 — 결함이 아니라 카드 후보(finalize가 처리)
            continue
        must = e.priority == "must"
        severity = "blocker" if (must and e.status == "missing") else ("major" if must else "minor")
        findings.append(
            Finding(
                layer="L2", severity=severity, req_id=e.req_id,
                location=(e.evidence[0] if e.evidence else None),
                message=f"[{e.req_id}] {e.status}: {e.note or ''}".strip(),
                fix_hint=e.note,
            )
        )
    for gap in report.scenario_gaps:
        findings.append(Finding(layer="L2", severity="minor", message=f"시나리오 공백: {gap}"))
    return findings


def from_simulation(report) -> list[Finding]:
    """L3 SimulationReport → Finding 목록. 실행 서사가 깨지는 경로는 major."""
    findings: list[Finding] = []
    for v in report.verdicts:
        if v.ok:
            continue
        for issue in v.issues or ["경로 판정 실패"]:
            findings.append(
                Finding(layer="L3", severity="major", message=f"[{v.trace_id}] {issue}")
            )
    return findings
