"""app/agent/recommend/plan.py 신호 탐지·fan-out 단위 테스트 (RPA-27).

LLM 없음. 컨테이너 강제 후보 감지, 패키지 프리맵, Send fan-out을 검증한다.
"""

from app.agent.recommend.plan import (
    detect_container_candidates,
    fan_out,
    prefmap_packages,
)


def test_detect_loop_from_branching():
    step = {"name": "엑셀 가공", "description": "", "branching": "최근 3일치만 반복"}
    assert ("Loop", "loop.commands.start") in detect_container_candidates(step)


def test_detect_if_from_condition_text():
    step = {"name": "분기", "description": "값이 없으면 건너뛴다", "branching": None}
    assert ("If", "if") in detect_container_candidates(step)


def test_no_container_for_plain_step():
    step = {"name": "네이버 접속", "description": "네이버에 접속한다", "branching": None}
    assert detect_container_candidates(step) == []


def test_prefmap_web_and_excel():
    assert "WebAutomation" in prefmap_packages({"name": "네이버 접속", "systems": ["Edge"]})
    assert "Excel_MS" in prefmap_packages({"name": "엑셀 가공", "description": "테두리 서식", "systems": ["Excel"]})


def test_prefmap_email_from_knox():
    assert "Email" in prefmap_packages({"name": "메일 발송", "systems": ["Knox Portal"]})


def test_fan_out_emits_one_send_per_step():
    state = {
        "analysis": {"steps": [
            {"step_id": "step-1", "name": "a"},
            {"step_id": "step-2", "name": "b"},
        ]},
        "constraints": ["c1"],
    }
    sends = fan_out(state)
    assert [s.node for s in sends] == ["step", "step"]
    assert sends[0].arg["order"] == 1 and sends[1].arg["order"] == 2
    assert sends[0].arg["constraints"] == ["c1"]


def test_fan_out_empty_when_no_steps():
    assert fan_out({"analysis": {"steps": []}}) == []
