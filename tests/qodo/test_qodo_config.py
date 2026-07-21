from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def _load_qodo_config() -> dict:
    with (ROOT / ".pr_agent.toml").open("rb") as config_file:
        return tomllib.load(config_file)


def test_qodo_config_is_parseable_and_fail_closed() -> None:
    qodo = _load_qodo_config()

    assert not (ROOT / ".coderabbit.yaml").exists()
    assert qodo["config"]["response_language"] == "ko-KR"
    assert qodo["config"]["enable_comment_approval"] is False
    assert qodo["config"]["enable_auto_approval"] is False
    assert qodo["github_app"]["feedback_on_draft_pr"] is False
    assert qodo["github_app"]["handle_push_trigger"] is True
    # 개선제안(/improve)은 "실제 에러"가 아니라 최적화·취향이라 자동 오픈에서 제외한다
    # (RPA-230). 필요할 때만 수동 /improve.
    assert set(qodo["github_app"]["pr_commands"]) == {"/review"}
    assert qodo["github_app"]["push_commands"] == ["/review"]
    assert "data/**" in qodo["ignore"]["glob"]


def test_qodo_review_policy_preserves_project_boundaries() -> None:
    qodo = _load_qodo_config()
    reviewer = qodo["pr_reviewer"]
    suggestions = qodo["pr_code_suggestions"]

    assert reviewer["require_tests_review"] is True
    assert reviewer["require_security_review"] is True
    assert reviewer["require_ticket_analysis_review"] is True
    assert reviewer["enable_review_labels_security"] is False
    assert reviewer["enable_review_labels_effort"] is False
    # 리뷰 범위를 심각·필수 결함으로 좁힘 (RPA-230) — 부가 메타 섹션(공수 추정·분할 제안)을
    # 끄고, 저심각·문서/주석·이론적 리스크는 지적하지 않도록 제외 규칙을 고정한다.
    # 누가 되돌리면(다시 true / 규칙 삭제) 이 래칫이 잡는다.
    assert reviewer["require_estimate_effort_to_review"] is False
    assert reviewer["require_can_be_split_review"] is False
    assert "지적하지 않는다" in reviewer["extra_instructions"]
    assert "이론적·저확률" in reviewer["extra_instructions"]
    assert suggestions["focus_only_on_problems"] is True
    assert suggestions["commitable_code_suggestions"] is False
    assert suggestions["apply_suggestions_checkbox"] is False
    # 제안 임계도 정책의 일부 — 되돌아가면 저점수 노이즈가 다시 샌다 (RPA-230 Qodo).
    assert suggestions["suggestions_score_threshold"] == 7

    instructions = reviewer["extra_instructions"] + suggestions["extra_instructions"]
    assert "app/agent/**" in instructions
    assert "API·SSE·스키마·DB·배포 계약" in instructions
    assert "시크릿" in instructions
    # 배포 환경 고려 (RPA-226 후속) — Qodo가 다중 인스턴스·secret 노출·CFN 재실행을
    # 리뷰 시 짚도록 지침이 살아 있는지 래칫으로 고정한다(누가 지우면 테스트가 잡는다).
    assert "다중 인스턴스" in instructions
    assert "xtrace" in instructions
