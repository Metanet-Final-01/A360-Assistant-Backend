"""Validate that a pull request body follows the repository template."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = (
    "관련 이슈",
    "무엇을 왜 변경했나요?",
    "주요 변경 사항",
    "확인 방법",
    "체크리스트",
)

REQUIRED_CHECKLIST_TERMS = {
    "PR 제목의 Jira 키 확인": ("pr 제목", "jira"),
    "자가 diff 리뷰": ("diff", "리뷰"),
    "시크릿·개인정보 확인": ("시크릿", "개인정보"),
}

HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
JIRA_RE = re.compile(r"\bRPA-\d+\b", re.IGNORECASE)
MIRROR_RE = re.compile(r"\bCloses\s+#\d+\b", re.IGNORECASE)
CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$", re.MULTILINE)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,}).*$")


def _sections(body: str) -> tuple[list[str], dict[str, str]]:
    matches = list(HEADING_RE.finditer(body))
    names = [match.group(1).strip() for match in matches]
    contents: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        contents[match.group(1).strip()] = body[match.end() : end]
    return names, contents


def _meaningful(text: str) -> str:
    text = HTML_COMMENT_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if FENCE_RE.match(stripped):
            continue
        if stripped in {"", "-", "- Jira:", "- GitHub Issue: Closes #"}:
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _has_mirror_exception(related: str) -> bool:
    cleaned = HTML_COMMENT_RE.sub("", related)
    for line in cleaned.splitlines():
        normalized = line.strip().lower()
        if not normalized:
            continue
        if "github issue" in normalized and any(
            marker in normalized
            for marker in ("없음", "대기", "미생성", "해당 없음", "n/a")
        ):
            return True
    return False


def validate_pr_body(body: str, title: str) -> list[str]:
    errors: list[str] = []
    body = body.lstrip("\ufeff")
    headings, sections = _sections(body)

    missing = [name for name in REQUIRED_SECTIONS if name not in headings]
    if missing:
        errors.append("필수 섹션 누락 또는 제목 불일치: " + ", ".join(missing))
        return errors

    positions = [headings.index(name) for name in REQUIRED_SECTIONS]
    if positions != sorted(positions):
        errors.append("필수 섹션 순서가 PR 템플릿과 다릅니다.")

    duplicates = [name for name in REQUIRED_SECTIONS if headings.count(name) > 1]
    if duplicates:
        errors.append("필수 섹션이 중복되었습니다: " + ", ".join(duplicates))

    title_keys = {key.upper() for key in JIRA_RE.findall(title)}
    related = sections["관련 이슈"]
    related_without_comments = HTML_COMMENT_RE.sub("", related)
    body_keys = {key.upper() for key in JIRA_RE.findall(related_without_comments)}
    if not title_keys:
        errors.append("PR 제목에 Jira 키(RPA-N)가 없습니다.")
    if not body_keys:
        errors.append("'관련 이슈' 섹션에 Jira 키(RPA-N)가 없습니다.")
    if title_keys and body_keys and title_keys.isdisjoint(body_keys):
        errors.append("PR 제목과 '관련 이슈' 섹션의 Jira 키가 일치하지 않습니다.")

    if not MIRROR_RE.search(related_without_comments) and not _has_mirror_exception(related):
        errors.append(
            "GitHub 미러 이슈를 'Closes #번호'로 연결하거나 "
            "'GitHub Issue: 없음 (사유)'을 적어주세요."
        )

    for name in ("무엇을 왜 변경했나요?", "주요 변경 사항", "확인 방법"):
        if not _meaningful(sections[name]):
            errors.append(f"'{name}' 섹션에 실제 내용을 작성해주세요.")

    checklist = sections["체크리스트"]
    checked_items = [
        label.lower()
        for mark, label in CHECKBOX_RE.findall(checklist)
        if mark.lower() == "x"
    ]
    for description, terms in REQUIRED_CHECKLIST_TERMS.items():
        matching = [item for item in checked_items if all(term in item for term in terms)]
        if not matching:
            errors.append(f"체크리스트 미완료: {description}")

    return errors


def _load_event(path: Path) -> tuple[str, str]:
    event = json.loads(path.read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") or {}
    return pull_request.get("body") or "", pull_request.get("title") or ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--event-path",
        type=Path,
        default=os.environ.get("GITHUB_EVENT_PATH"),
        help="GitHub pull_request event JSON path",
    )
    args = parser.parse_args()
    if args.event_path is None:
        parser.error("--event-path or GITHUB_EVENT_PATH is required")

    body, title = _load_event(args.event_path)
    errors = validate_pr_body(body, title)
    if not errors:
        print("PR 본문이 저장소 템플릿을 준수합니다.")
        return 0

    print("PR 본문 템플릿 검사가 실패했습니다:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
