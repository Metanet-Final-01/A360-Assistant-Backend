"""실행 환경 검사(R15~R16)·세션 레지스트리 v2 신호 (RPA-206 소비부).

R15: 트리거 자동 실행 흐름의 대화형 액션(attended 함정) — warning.
R16: platform 메타(windows=False) 패키지 사용 — warning.
세션: v2 카탈로그의 session_role 신호를 derive_session_registry가 읽는다.
"""

from app.agent.v3.verify.checker import (
    SESSION_OPENERS,
    _is_session_param,
    derive_session_registry,
    run_environment_checks,
)


class _Cat:
    def __init__(self, specs):
        self._specs = specs
        self._by_key = {(s["package"], s["action"]): s for s in specs}

    def get_action_schema(self, package, action):
        return self._by_key.get((package, action))

    def iter_action_schemas(self):
        yield from self._specs


def _flow(actions, trigger=None):
    return {"steps": [{"step_id": "step-1", "actions": actions}], "trigger": trigger}


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
