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
    assert set(qodo["github_app"]["pr_commands"]) == {"/review", "/improve"}
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
    assert suggestions["focus_only_on_problems"] is True
    assert suggestions["commitable_code_suggestions"] is False
    assert suggestions["apply_suggestions_checkbox"] is False

    instructions = reviewer["extra_instructions"] + suggestions["extra_instructions"]
    assert "app/agent/**" in instructions
    assert "API·SSE·스키마·DB·배포 계약" in instructions
    assert "시크릿" in instructions
