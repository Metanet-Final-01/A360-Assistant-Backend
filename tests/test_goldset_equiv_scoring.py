# -*- coding: utf-8 -*-
"""기능 등가 채점 축 (RPA-298).

**왜 이 축이 생겼나.** 엄격 채점은 `pkg_key`가 다르면 유사도를 0으로 하드 게이트한다.
그래서 정답 봇이 `Email/emailConnect`로 한 일을 에이전트가 `Microsoft 365 Outlook/Connect`
로 하면 0점이다. 우리가 정한 정답 기준은 "비전문가가 흐름도를 보고 Control Room에 손으로
넣으면 **돌아간다**"이므로 두 경로 모두 성공인데, 지표가 그걸 못 쟀다.

**이 축의 위험은 반대쪽이다 — 너무 헐거워져 아무거나 맞는 것.** 여기서 지키는 건 셋이다.

1. **완화만 한다** — 엄격 축에서 맞던 쌍은 등가 축에서도 반드시 맞는다(단조).
   등가 f1 < 엄격 f1이 나오면 도메인 표가 뭔가를 깨뜨린 것이다.
2. **자원이 다르면 안 묶인다** — OCR↔Recorder, Google Sheets↔Excel advanced 같은
   '비슷해 보이지만 사람이 옮기면 결과가 다른' 쌍은 여전히 0점이어야 한다.
3. **하는 일이 다르면 안 맞는다** — 도메인이 같아도 Connect↔Send는 토큰이 안 겹쳐
   여전히 불일치다. 도메인은 게이트일 뿐 매칭 근거가 아니다.
"""

import pytest

from scripts.goldset_eval.metrics import greedy_match, score_case
from scripts.goldset_eval.notation import (
    MATCH_THRESHOLD,
    CanonAction,
    capability_domain,
    similarity,
)


def _sc(pred, gold, kb=None):
    return score_case(pred, gold, kb or [])


# ── (A) 완화 방향 — 엄격에서 맞던 건 등가에서도 맞는다 ───────────────────────

@pytest.mark.parametrize("pkg,act", [
    ("Excel_MS", "OpenSpreadsheet"),
    ("Email", "emailConnect"),
    ("If", "if"),
    ("Loop", "loop.commands.start"),
    ("Rest", "restPost"),
])
def test_identical_actions_match_on_both_axes(pkg, act):
    s = _sc([(pkg, act)], [(pkg, act)])
    assert s["action"]["f1"] == 1.0
    assert s["action_equiv"]["f1"] == 1.0
    assert s["action_equiv"]["n_cross_package"] == 0, "같은 패키지는 교차로 세지 않는다"


def test_equivalence_never_scores_below_strict():
    """단조성 — 등가는 엄격의 완화라 매칭이 줄어들 수 없다.

    실제 골드셋에서 나온 혼합 시퀀스로 확인한다. 이게 깨지면 도메인 표가 기존에
    맞던 쌍을 다른 골드 액션에 뺏기게 만든 것이다(탐욕 매칭의 부작용).
    """
    gold = [("Email", "emailConnect"), ("Email", "saveAttachment"), ("Email", "closeEmail"),
            ("Excel_MS", "OpenSpreadsheet"), ("Excel_MS", "GetMultipleCells"),
            ("Rest", "restPost"), ("If", "if"), ("String", "assign")]
    pred = [("Microsoft 365 Outlook", "Connect"), ("Microsoft 365 Outlook", "Save all attachments"),
            ("Microsoft 365 Excel", "Open"), ("If", "If"), ("Jira", "Create project")]
    s = _sc(pred, gold)
    assert s["action_equiv"]["f1"] >= s["action"]["f1"]
    assert s["action_equiv"]["n_matched"] >= s["n_matched"]


# ── (B) 실측에서 나온 대안 경로가 실제로 잡히는가 ────────────────────────────

@pytest.mark.parametrize("gold_pkg,gold_act,pred_pkg,pred_act,domain", [
    # 케이스 01·11 실측: 정답은 범용 Email, 에이전트는 M365 Outlook
    ("Email", "emailConnect", "Microsoft 365 Outlook", "Connect", "mail"),
    ("Email", "saveAttachment", "Microsoft 365 Outlook", "Save all attachments", "mail"),
    # 케이스 01 실측: 데스크톱 Excel ↔ M365 Excel
    ("Excel_MS", "OpenSpreadsheet", "Microsoft 365 Excel", "Open", "spreadsheet"),
    ("Excel_MS", "CloseSpreadsheet", "Excel basic", "Close", "spreadsheet"),
])
def test_alternative_package_paths_are_credited(gold_pkg, gold_act, pred_pkg, pred_act, domain):
    s = _sc([(pred_pkg, pred_act)], [(gold_pkg, gold_act)])
    assert s["action"]["f1"] == 0.0, "엄격 축에서는 여전히 불일치여야 한다(기준선 보존)"
    assert s["action_equiv"]["f1"] == 1.0
    assert s["action_equiv"]["n_cross_package"] == 1
    assert s["equiv_pairs"][0]["domain"] == domain


# ── (C) 헐거워지면 안 되는 경계 ──────────────────────────────────────────────

@pytest.mark.parametrize("gold,pred,why", [
    (("Recorder", "capture"), ("OCR", "Capture area"),
     "픽셀 판독과 UI 객체 제어는 사람이 옮기면 결과가 다르다"),
    (("Excel_MS", "OpenSpreadsheet"), ("Google Sheets", "Open spreadsheet"),
     "Google Sheets는 로컬 .xlsx 경로를 못 연다"),
    (("Excel_MS", "OpenSpreadsheet"), ("Apple Numbers", "Open"),
     "Apple Numbers도 같은 이유 — 백엔드가 다르다"),
])
def test_lookalike_resources_are_held_apart_by_the_domain_gate(gold, pred, why):
    """🔴 **도메인 게이트가 실제로 일하는지** 증명하는 테스트.

    이 쌍들은 액션 토큰이 임계 이상으로 겹친다(capture↔capture, open↔open) — 즉 도메인만
    같았으면 **맞아 버린다**. 토큰이 안 겹쳐서 우연히 통과하는 게 아님을 먼저 확인하고,
    그 위에서 등가 축이 0점임을 본다. 표에 이 패키지를 잘못 추가하면 여기가 먼저 깨진다.
    """
    gc, pc = CanonAction(*gold), CanonAction(*pred)
    assert similarity(gc.tokens, pc.tokens) >= MATCH_THRESHOLD, \
        f"전제 실패: 토큰이 애초에 안 겹쳐 이 테스트가 게이트를 검증하지 못한다 ({gc} vs {pc})"
    assert gc.domain != pc.domain, why
    assert _sc([pred], [gold])["action_equiv"]["f1"] == 0.0, why


@pytest.mark.parametrize("gold,pred,why", [
    (("DLL", "RunCSharpDLL_V1"), ("Python Script", "Execute script"),
     "'외부 코드 실행'으로 묶으면 사람이 옮길 게 완전히 달라진다"),
    (("Rest", "restPost"), ("SOAP Web Service", "Invoke"),
     "전송 프로토콜이 다르면 엔드포인트 구성이 다르다"),
    (("Email", "emailConnect"), ("Slack", "Send message"),
     "메일과 채팅은 도착지가 다르다"),
])
def test_unrelated_actions_stay_unmatched(gold, pred, why):
    """도메인·토큰 어느 쪽으로도 안 맞아야 하는 쌍 — 표를 늘려도 여기가 뚫리면 안 된다."""
    assert _sc([pred], [gold])["action_equiv"]["f1"] == 0.0, why


def test_same_domain_different_operation_still_unmatched():
    """도메인은 **게이트**일 뿐 매칭 근거가 아니다 — 하는 일이 다르면 여전히 불일치."""
    s = _sc([("Microsoft 365 Outlook", "Send email")], [("Email", "emailConnect")])
    assert s["action_equiv"]["f1"] == 0.0
    assert s["action_equiv"]["n_cross_package"] == 0


def test_domain_table_only_groups_declared_packages():
    """표에 없는 패키지는 도메인이 곧 자기 자신 — 등가 축이 엄격 축과 같아야 한다."""
    for pkg in ("string", "if", "loop", "rest", "recorder", "ocr", "dll", "twilio"):
        assert capability_domain(pkg) == pkg


def test_generic_rest_has_no_domain():
    """REST는 **범용 전송**이라 도메인을 주지 않는다 (RPA-298 미해결 항목).

    정답의 `Rest/restPost`가 JIRA를 치는지 Slack을 치는지는 URL 파라미터를 봐야 알고,
    패키지 이름만으로는 알 수 없다. 여기에 도메인을 주면 모든 API 호출이 서로 등가가 된다.
    실측 케이스 01의 `Rest/restPost` ↔ `Jira/Create project`는 그래서 **아직 안 잡힌다** —
    이 테스트가 그 사실을 명시적으로 붙들어 둔다(조용히 잊히지 않게).
    """
    assert capability_domain("rest") == "rest"
    s = _sc([("Jira", "Create project")], [("Rest", "restPost")])
    assert s["action_equiv"]["f1"] == 0.0


# ── (D) 매칭기 자체 ──────────────────────────────────────────────────────────

def test_greedy_match_equivalent_flag_switches_the_gate():
    pred = [CanonAction("Microsoft 365 Outlook", "Connect")]
    gold = [CanonAction("Email", "emailConnect")]
    assert greedy_match(pred, gold).pairs == []
    assert len(greedy_match(pred, gold, equivalent=True).pairs) == 1


def test_equiv_pairs_records_both_sides_for_audit():
    """도메인 표는 사람 판단이라 무엇이 발동했는지 남아야 사후 감사가 된다."""
    s = _sc([("Microsoft 365 Outlook", "Connect")], [("Email", "emailConnect")])
    p = s["equiv_pairs"][0]
    assert p["gold"] == ["Email", "emailConnect"]
    assert p["pred"] == ["Microsoft 365 Outlook", "Connect"]
    assert p["sim"] >= 0.55


def test_empty_sequences_do_not_crash():
    for pred, gold in (([], []), ([("If", "if")], []), ([], [("If", "if")])):
        s = _sc(pred, gold)
        assert s["action_equiv"]["n_cross_package"] == 0
