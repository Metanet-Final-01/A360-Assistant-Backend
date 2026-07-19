"""recommend_trigger — 시점 의도 게이트·전량 메뉴·폐쇄어휘 (A-2, RPA-206 소비부)."""

import app.agent.v3.orchestrator.triggers as trig_mod


class _Cat:
    def __init__(self, rows):
        self._rows = rows

    def list_trigger_schemas(self):
        return self._rows


_ROWS = [
    {"package": "Email trigger", "title": "Creating an email trigger", "url": "/x", "content": "메일 수신 시 봇 실행"},
    {"package": "File Folder trigger", "title": "Creating a file and folder trigger", "url": "/y", "content": "파일/폴더 변화 감지"},
]


def _pick(**kw):
    d = {"none": False, "kind": "trigger", "package": "Email trigger", "title": "Creating an email trigger",
         "reason": "'메일이 오면' 요구", "setup_hint": "Control Room에서 이메일 트리거 연결"}
    d.update(kw)
    return trig_mod._TriggerPick(**d)


def test_no_intent_skips_llm(monkeypatch):
    # 시점 표현이 없으면 LLM을 부르지 않는다 (결정론 게이트, 비용 0)
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Cat(_ROWS))

    def _boom(*a, **k):
        raise AssertionError("의도 없음이면 LLM을 부르면 안 된다")

    monkeypatch.setattr(trig_mod, "chat_json", _boom)
    assert trig_mod.recommend_trigger({"goal": "엑셀 자료 정리", "requirements": []}, None) is None


def test_event_intent_picks_from_menu(monkeypatch):
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Cat(_ROWS))
    monkeypatch.setattr(trig_mod, "chat_json", lambda *a, **k: _pick())
    out = trig_mod.recommend_trigger({"goal": "메일이 오면 첨부를 저장한다", "requirements": []}, None)
    assert out["kind"] == "trigger"
    assert out["package"] == "Email trigger"
    assert out["sources"][0]["source_type"] == "trigger_schema"


def test_pick_outside_menu_rejected(monkeypatch):
    # 폐쇄어휘 — 메뉴에 없는 트리거 패키지를 지어내면 제안을 버린다
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Cat(_ROWS))
    monkeypatch.setattr(trig_mod, "chat_json", lambda *a, **k: _pick(package="Invented trigger"))
    assert trig_mod.recommend_trigger({"goal": "메일이 오면 처리한다", "requirements": []}, None) is None


def test_time_intent_becomes_schedule(monkeypatch):
    # 시간 기반은 트리거 행이 없어도(구 카탈로그) Control Room 스케줄로 제안 가능
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Cat([]))
    monkeypatch.setattr(
        trig_mod, "chat_json",
        lambda *a, **k: _pick(kind="schedule", package=None, title="Control Room 예약 실행"),
    )
    out = trig_mod.recommend_trigger({"goal": "매일 아침 9시에 보고서를 만든다", "requirements": []}, None)
    assert out["kind"] == "schedule"
    assert out["package"] is None
    assert out["sources"] == []


def test_llm_none_or_failure_degrades_silently(monkeypatch):
    monkeypatch.setattr(trig_mod, "get_catalog", lambda: _Cat(_ROWS))
    monkeypatch.setattr(trig_mod, "chat_json", lambda *a, **k: _pick(none=True))
    assert trig_mod.recommend_trigger({"goal": "매일 정산한다", "requirements": []}, None) is None

    def _fail(*a, **k):
        raise ValueError("llm down")

    monkeypatch.setattr(trig_mod, "chat_json", _fail)
    assert trig_mod.recommend_trigger({"goal": "매일 정산한다", "requirements": []}, None) is None
