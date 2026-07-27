"""실행 환경 검사(R15~R16)·세션 레지스트리 v2 신호 (RPA-206 소비부).

R15: 트리거 자동 실행 흐름의 대화형 액션(attended 함정) — warning.
R16: platform 메타(windows=False) 패키지 사용 — warning.
세션: v2 카탈로그의 session_role 신호를 derive_session_registry가 읽는다.
"""

from app.agent.v3.verify.checker import (
    DEFAULT_TARGET_OS,
    SESSION_OPENERS,
    _is_session_param,
    derive_session_registry,
    run_environment_checks,
    target_os,
)


class _Cat:
    def __init__(self, specs):
        self._specs = specs
        self._by_key = {(s["package"], s["action"]): s for s in specs}

    def get_action_schema(self, package, action):
        return self._by_key.get((package, action))

    def iter_action_schemas(self):
        yield from self._specs


def _flow(actions, trigger=None, assumptions=None):
    flow = {"steps": [{"step_id": "step-1", "actions": actions}], "trigger": trigger}
    if assumptions is not None:
        flow["spec"] = {"assumptions": assumptions}
    return flow


def test_r15_only_when_trigger_present():
    act = {"package": "Message Box", "action": "Message box", "parameters": [], "children": []}
    cat = _Cat([])
    assert run_environment_checks(_flow([act]), cat) == []
    v = run_environment_checks(_flow([act], trigger={"kind": "trigger", "title": "Email trigger"}), cat)
    assert [x.rule for x in v] == ["R15"]
    assert v[0].severity == "warning"


def test_r16_warns_on_windows_unsupported_package():
    spec = {"package": "Apple Mail", "action": "Send email", "platform": {"macos": True, "windows": False}}
    act = {"package": "Apple Mail", "action": "Send email", "parameters": [], "children": []}
    v = run_environment_checks(_flow([act]), _Cat([spec]))
    assert [x.rule for x in v] == ["R16"]
    assert v[0].severity == "warning"


def test_r16_silent_when_platform_ok_or_absent():
    specs = [
        {"package": "Excel advanced", "action": "Open", "platform": {"macos": True, "windows": True}},
        {"package": "Email", "action": "Send"},
    ]
    acts = [
        {"package": "Excel advanced", "action": "Open", "parameters": [], "children": []},
        {"package": "Email", "action": "Send", "parameters": [], "children": []},
    ]
    assert run_environment_checks(_flow(acts), _Cat(specs)) == []


def test_session_registry_reads_v2_session_role():
    # v2 문서 카탈로그의 명시 신호 — return_type(구 JAR) 없이도 opener/closer가 유도된다.
    specs = [
        {"package": "Google Sheets", "action": "Open spreadsheet", "session_role": "opener"},
        {"package": "Google Sheets", "action": "Close", "session_role": "closer"},
        {"package": "Google Sheets", "action": "Set cell"},
    ]
    openers, closers = derive_session_registry(_Cat(specs))
    assert ("Google Sheets", "Open spreadsheet") in openers
    assert ("Google Sheets", "Close") in closers
    assert set(SESSION_OPENERS) <= set(openers)  # 수기 상수 유지 — 구 카탈로그 호환


def test_session_param_predicate_covers_doc_label_notation():
    assert _is_session_param("session") and _is_session_param("sessionName")
    assert _is_session_param("Session name")  # v2 문서 라벨 표기
    assert not _is_session_param("File path")


# ─────────────────────────────────────────────────────────────────────────────
# R16 방향 인식 — 대상 OS는 흐름도 전제(spec.assumptions)에서 읽는다 (RPA-282)
# ─────────────────────────────────────────────────────────────────────────────

_APPLE = {"package": "Apple Mail", "action": "Send email", "platform": {"macos": True, "windows": False}}
_WINONLY = {"package": "Active Directory", "action": "Connect", "platform": {"macos": False, "windows": True}}


def _act_of(spec):
    return {"package": spec["package"], "action": spec["action"], "parameters": [], "children": []}


def _msg_names_os(violation, os_label):
    """경고 문구가 실제 대상 OS를 말하는지 — 방향이 바뀌었는데 문구가 그대로면 오해를 부른다."""
    return os_label in violation.message and violation.severity == "warning"


def test_target_os_defaults_to_windows_without_spec():
    """전제가 없는 (구)흐름도는 기존과 똑같이 Windows 가정으로 판정된다 — 하위호환."""
    assert target_os({}) == DEFAULT_TARGET_OS == "windows"
    assert target_os({"spec": {"assumptions": []}}) == "windows"


def test_target_os_reads_env_assumption():
    assert target_os({"spec": {"assumptions": ["실행 환경: macOS 러너 (사용자 명시)"]}}) == "macos"
    assert target_os({"spec": {"assumptions": ["실행 환경: Windows 러너 (명시 없어 가정)"]}}) == "windows"
    # 맥OS 한글 표기도 집는다
    assert target_os({"spec": {"assumptions": ["실행 환경: 맥OS 러너"]}}) == "macos"


def test_target_os_ignores_non_environment_assumptions():
    """아무 전제에서나 OS 단어를 줍지 않는다 — '실행 환경' 줄만 본다."""
    flow = {"spec": {"assumptions": ["Windows 공유폴더 경로를 사용한다고 가정", "시트는 첫 번째 시트"]}}
    assert target_os(flow) == "windows"  # 기본값이지 이 문장에서 읽은 게 아니다
    flow2 = {"spec": {"assumptions": ["macOS 사용자에게도 메일을 보낸다고 가정"]}}
    assert target_os(flow2) == "windows"  # 대상 OS 선언이 아니므로 기본값 유지


def test_r16_flips_direction_when_target_is_macos():
    """대상이 macOS면 경고 방향이 뒤집힌다 — Apple 계열은 통과, Windows 전용이 걸린다."""
    specs, mac_assume = [_APPLE, _WINONLY], ["실행 환경: macOS 러너 (사용자 명시)"]

    # Apple 계열: Windows 대상에서는 경고, macOS 대상에서는 침묵
    assert [v.rule for v in run_environment_checks(_flow([_act_of(_APPLE)]), _Cat(specs))] == ["R16"]
    assert run_environment_checks(_flow([_act_of(_APPLE)], assumptions=mac_assume), _Cat(specs)) == []

    # Windows 전용: 정반대
    assert run_environment_checks(_flow([_act_of(_WINONLY)]), _Cat(specs)) == []
    v = run_environment_checks(_flow([_act_of(_WINONLY)], assumptions=mac_assume), _Cat(specs))
    assert [x.rule for x in v] == ["R16"]
    assert _msg_names_os(v[0], "macOS")


def test_r16_silent_when_target_key_absent_from_platform_meta():
    """platform 메타에 대상 OS 키가 없으면 침묵한다 (모름 → 침묵)."""
    spec = {"package": "Email", "action": "Send", "platform": {"windows": True}}  # macos 키 없음
    flow = _flow([_act_of(spec)], assumptions=["실행 환경: macOS 러너"])
    assert run_environment_checks(flow, _Cat([spec])) == []
