"""관측 로그 PII 마스킹 테스트 (RPA-123)."""

from app.core.masking import mask_fields, mask_pii


def test_mask_email():
    assert mask_pii("문의는 user@example.com 으로") == "문의는 [EMAIL] 으로"


def test_mask_long_number_but_not_short():
    assert "[NUM]" in mask_pii("연락처 010-1234-5678 입니다")   # 전화번호 후보
    assert mask_pii("Task4 단계 3개, CHUNK 1200") == "Task4 단계 3개, CHUNK 1200"  # 짧은 숫자는 보존


def test_mask_none_and_empty_passthrough():
    assert mask_pii(None) is None
    assert mask_pii("") == ""


def test_mask_fields_only_targets_and_immutable():
    data = {"route": "qa", "reason": "user@x.com 관련 문의", "step_id": "s1"}
    out = mask_fields(data, ("reason", "query"))
    assert out["reason"] == "[EMAIL] 관련 문의"      # 대상 키만 마스킹
    assert out["route"] == "qa" and out["step_id"] == "s1"  # 구조값 불변
    assert data["reason"] == "user@x.com 관련 문의"   # 원본 dict 불변
