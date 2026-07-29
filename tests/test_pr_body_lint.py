from pathlib import Path

from scripts.validate_pr_body import validate_pr_body

ROOT = Path(__file__).resolve().parents[1]


VALID_BODY = """\
## 관련 이슈

- Jira: RPA-315
- GitHub Issue: Closes #420

## 무엇을 왜 변경했나요?

PR 본문이 깨져도 제목 검사만 통과하던 문제를 막습니다.

## 주요 변경 사항

- PR 본문 템플릿 검사기를 추가했습니다.

## 확인 방법

```bash
pytest tests/test_pr_body_lint.py -q
```

## 체크리스트

- [x] PR 제목에 Jira 키가 포함되어 있습니다.
- [x] 스스로 diff를 리뷰했습니다.
- [x] 시크릿·개인정보가 포함되지 않았습니다.
"""


def test_accepts_valid_template_body() -> None:
    assert validate_pr_body(
        VALID_BODY,
        "ci: PR 본문 템플릿 검사 추가 (RPA-315)",
    ) == []


def test_accepts_explicit_missing_mirror_reason() -> None:
    body = VALID_BODY.replace(
        "- GitHub Issue: Closes #420",
        "- GitHub Issue: 없음 (Jira 자동화 미생성 대기)",
    )

    assert validate_pr_body(body, "ci: 본문 검사 (RPA-315)") == []


def test_rejects_mojibake_headings_from_regression_pr() -> None:
    body = """\
﻿## 愿^@???댁뒋

- Jira: RPA-268
- GitHub Issue: Closes #72

## 臾댁뾿??蹂^@寃쏀뻽?섏슂?

변경 설명

## 二쇱슂 蹂^@寃??ы빆

- 변경 사항

## ?뺤씤 諛⑸쾿

- 테스트

## 泥댄겕由ъ뒪??

- [x] PR 제목에 Jira 키가 포함되어 있습니다.
"""

    errors = validate_pr_body(
        body,
        "feat(infra): Backend CloudFormation 분리 (RPA-268)",
    )

    assert errors
    assert errors[0].startswith("필수 섹션 누락 또는 제목 불일치")


def test_rejects_empty_template_placeholders() -> None:
    body = """\
## 관련 이슈

<!-- Jira: RPA-12 / GitHub 미러 이슈: Closes #12 -->

- Jira:
- GitHub Issue: Closes #

## 무엇을 왜 변경했나요?

<!-- 설명 -->

## 주요 변경 사항

-

## 확인 방법

```bash

```

## 체크리스트

- [ ] PR 제목에 Jira 키가 포함되어 있습니다.
- [ ] 스스로 diff를 리뷰했습니다.
- [ ] 시크릿·개인정보가 포함되지 않았습니다.
"""

    errors = validate_pr_body(body, "ci: 본문 검사")

    assert "PR 제목에 Jira 키(RPA-N)가 없습니다." in errors
    assert "'관련 이슈' 섹션에 Jira 키(RPA-N)가 없습니다." in errors
    assert any("GitHub 미러 이슈" in error for error in errors)
    assert "'무엇을 왜 변경했나요?' 섹션에 실제 내용을 작성해주세요." in errors
    assert "'주요 변경 사항' 섹션에 실제 내용을 작성해주세요." in errors
    assert "'확인 방법' 섹션에 실제 내용을 작성해주세요." in errors


def test_rejects_mismatched_jira_keys_and_unchecked_required_items() -> None:
    body = VALID_BODY.replace("RPA-315", "RPA-999").replace(
        "- [x] 스스로 diff를 리뷰했습니다.",
        "- [ ] 스스로 diff를 리뷰했습니다.",
    )

    errors = validate_pr_body(body, "ci: 본문 검사 (RPA-315)")

    assert "PR 제목과 '관련 이슈' 섹션의 Jira 키가 일치하지 않습니다." in errors
    assert "체크리스트 미완료: 자가 diff 리뷰" in errors


def test_rejects_empty_non_bash_fenced_validation_section() -> None:
    body = VALID_BODY.replace(
        "```bash\npytest tests/test_pr_body_lint.py -q\n```",
        "```sh\n\n```",
    )

    errors = validate_pr_body(body, "ci: 본문 검사 (RPA-315)")

    assert "'확인 방법' 섹션에 실제 내용을 작성해주세요." in errors


def test_workflow_checks_body_on_pr_edits_with_read_only_permissions() -> None:
    workflow = (ROOT / ".github/workflows/pr-title-lint.yml").read_text(
        encoding="utf-8"
    )

    assert "types: [opened, edited, synchronize, reopened]" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "python scripts/validate_pr_body.py" in workflow
