"""관측 로그 PII 마스킹 테스트 (RPA-123)."""

from app.core.masking import mask_fields, mask_pii


def test_mask_email():
    assert mask_pii("문의는 user@example.com 으로") == "문의는 [EMAIL] 으로"


def test_mask_long_number_but_not_short():
    assert "[NUM]" in mask_pii("연락처 010-1234-5678 입니다")   # 전화번호 후보
    assert mask_pii("Task4 단계 3개, CHUNK 1200") == "Task4 단계 3개, CHUNK 1200"  # 짧은 숫자는 보존


def test_mask_number_adjacent_to_korean_fully():
    """한글에 붙은 번호도 앞자리까지 통째로 마스킹 (CodeRabbit #184)."""
    assert mask_pii("연락처010-1234-5678") == "연락처[NUM]"  # 010이 안 남아야 함


def test_mask_six_digits_not_masked():
    """순수 숫자 7자리 미만(예: 123-456=6자리)은 마스킹 안 함 (CodeRabbit #184)."""
    assert mask_pii("코드 123-456 참고") == "코드 123-456 참고"
    assert "[NUM]" in mask_pii("주민 123-4567 형태")  # 7자리면 마스킹


def test_mask_none_and_empty_passthrough():
    assert mask_pii(None) is None
    assert mask_pii("") == ""


def test_mask_fields_only_targets_and_immutable():
    data = {"route": "qa", "reason": "user@x.com 관련 문의", "step_id": "s1"}
    out = mask_fields(data, ("reason", "query"))
    assert out["reason"] == "[EMAIL] 관련 문의"      # 대상 키만 마스킹
    assert out["route"] == "qa" and out["step_id"] == "s1"  # 구조값 불변
    assert data["reason"] == "user@x.com 관련 문의"   # 원본 dict 불변


def test_mask_fields_empty_returns_new_dict():
    """빈 dict도 새 객체 반환 — 원본 불변 계약 (CodeRabbit #184)."""
    src = {}
    out = mask_fields(src, ("reason",))
    assert out == {} and out is not src   # 같은 객체가 아니어야 함
    assert mask_fields(None, ("reason",)) is None  # None은 그대로
