"""Operator-facing explanations for Change Assurance control results."""
from __future__ import annotations

from typing import Any


_CONTROL_GUIDANCE = {
    "CH-01": {
        "impact": "신뢰할 수 있는 Git 변경 목록을 만들지 못해 이후 통제의 검사 대상을 확정할 수 없습니다.",
        "action": "PR의 base/head SHA와 체크아웃 상태를 확인한 뒤 Change Assurance를 다시 실행하세요.",
    },
    "CH-02": {
        "impact": "변경 경로에 맞는 위험 프로필을 확정하지 못해 필요한 검사가 빠질 수 있습니다.",
        "action": "변경 파일 경로와 위험 프로필 정책을 확인한 뒤 다시 실행하세요.",
    },
    "CH-04": {
        "impact": "추가·변경된 의존성의 선언, 고정 버전, 취약점, 라이선스 또는 설치 경로를 보증할 수 없습니다.",
        "action": "아래 세부 근거를 확인해 직접 의존성 선언·정확한 버전 고정·승인 정책을 보완하세요.",
    },
    "CH-06": {
        "impact": "테스트·워크플로·보증 정책 등 판정 기준 자체가 바뀌어 자동 검사만으로 독립성을 보증할 수 없습니다.",
        "action": "PR 최신 커밋에 대해 작성자와 다른 사람이 GitHub Approve 리뷰를 제출하세요.",
    },
    "CH-11": {
        "impact": "검사한 커밋과 실제 PR 최신 커밋이 다를 수 있어 현재 판정을 PR에 안전하게 결속할 수 없습니다.",
        "action": "PR 최신 HEAD가 체크아웃됐고 추적 파일 변경이 없는지 확인한 뒤 다시 실행하세요.",
    },
    "CH-12": {
        "impact": "증거 파일의 누락 또는 지문 불일치로 판정 기록의 무결성을 보증할 수 없습니다.",
        "action": "artifact 생성·업로드 과정과 SHA-256 검증 결과를 확인한 뒤 다시 실행하세요.",
    },
}


def _dependency_findings(evidence: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    rules = evidence.get("rules")
    if isinstance(rules, dict):
        for rule_id, rule in sorted(rules.items()):
            if not isinstance(rule, dict) or rule.get("status") in {
                "pass",
                "not_applicable",
            }:
                continue
            reasons = rule.get("reasons")
            if isinstance(reasons, list):
                for reason in reasons:
                    if isinstance(reason, str) and reason.strip():
                        findings.append(f"{rule_id}: {reason.strip()}")
    return findings


def _protected_finding(evidence: dict[str, Any]) -> str:
    paths = evidence.get("protected_paths_changed")
    path_values = [item for item in paths or [] if isinstance(item, str)]
    indicators = evidence.get("sensitive_indicators")
    indicator_values = [
        f"{item.get('path')} ({item.get('code')})"
        for item in indicators or []
        if isinstance(item, dict) and item.get("path") and item.get("code")
    ]
    review = evidence.get("human_review")
    review_reason = review.get("reason_code") if isinstance(review, dict) else None
    parts = []
    if path_values:
        parts.append("보호 경로 변경: " + ", ".join(path_values[:5]))
    if indicator_values:
        parts.append("민감 변경 신호: " + ", ".join(indicator_values[:5]))
    if review_reason:
        parts.append(f"사람 검토 상태: {review_reason}")
    return " / ".join(parts)


def explain_control(
    control_id: str,
    *,
    status: str,
    default_reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Return bounded, evidence-based guidance for a non-passing control."""
    if status in {"pass", "not_applicable"}:
        return None
    guidance = _CONTROL_GUIDANCE.get(
        control_id,
        {
            "impact": "이 통제를 통과하지 못해 전체 변경을 보증할 수 없습니다.",
            "action": "원본 증거와 사유 코드를 확인한 뒤 다시 실행하세요.",
        },
    )
    evidence = evidence or {}
    finding = ""
    if control_id == "CH-04":
        details = _dependency_findings(evidence)
        if details:
            finding = " / ".join(details[:8])
    elif control_id == "CH-06":
        finding = _protected_finding(evidence)
    if not finding:
        finding = default_reason
    return {
        "finding": finding[:1200],
        "impact": guidance["impact"],
        "action": guidance["action"],
    }
