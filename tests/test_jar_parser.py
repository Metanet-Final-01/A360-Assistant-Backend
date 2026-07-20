"""app/rag/sources/jar_parser.py 단위 테스트.

실측(WebAutomation 커뮤니티 JAR): 같은 액션 이름이 파라미터 개수가 다른 두 버전으로
package.json의 commands 배열 자체에 중복 정의돼 있었다 — _dedupe_actions_by_name이
파싱 시점에 파라미터가 더 많은(더 완전한) 쪽을 채택해 정리하는지 확인한다.

실측(GitHub 수집): 같은 패키지(예: Number)가 서로 다른 시기에 만들어진 여러 봇
저장소에 서로 다른 버전(v2.0.0~v3.8.0)으로 번들되어 있어, 병합 시 버전 비교 없이
마지막에 처리된 것을 그냥 덮어쓰면 최신이 아닌 버전이 채택되는 문제가 있었다 —
select_better_version이 실제로 더 높은 버전을 채택하는지 확인한다.
"""

from app.rag.sources.jar_parser import (
    _dedupe_actions_by_name,
    _select_latest_per_package,
    _version_sort_key,
    select_better_version,
)


def _action(name, param_count, label="l"):
    return {"name": name, "label": label, "parameters": [{"name": f"p{i}"} for i in range(param_count)]}


def test_no_duplicates_passes_through_unchanged():
    actions = [_action("a", 2), _action("b", 3)]
    result = _dedupe_actions_by_name(actions, "Pkg", "pkg.jar")
    assert result == actions


def test_duplicate_name_keeps_the_richer_parameter_variant():
    richer = _action("StartSessionWebAutomation", 9)
    poorer = _action("StartSessionWebAutomation", 5)
    result = _dedupe_actions_by_name([poorer, richer], "WebAutomation", "web.jar")
    assert len(result) == 1
    assert result[0] is richer


def test_duplicate_name_order_independent_richer_always_wins():
    richer = _action("clickelement", 6)
    poorer = _action("clickelement", 4)
    result = _dedupe_actions_by_name([richer, poorer], "WebAutomation", "web.jar")
    assert len(result) == 1
    assert result[0] is richer


def test_duplicate_logs_even_when_new_action_is_not_richer(capsys):
    # 실측 버그: 파라미터 개수가 같거나(순수 중복) 새 액션 쪽이 더 적으면
    # 예전 코드는 로그 없이 조용히 넘어갔다 — 이 함수의 목적(중복 발견 가시화)에
    # 어긋난다. 승패와 무관하게 항상 로그가 남아야 한다.
    richer_first = _action("checkelement", 5)
    poorer_second = _action("checkelement", 5)  # 완전 동일 중복(WebAutomation 17개 중 7개 패턴)
    _dedupe_actions_by_name([richer_first, poorer_second], "WebAutomation", "web.jar")
    assert "중복 정의 발견" in capsys.readouterr().out


def test_duplicate_logs_when_new_action_arrives_with_fewer_parameters(capsys):
    richer_first = _action("clickelement", 6)
    poorer_second = _action("clickelement", 4)
    _dedupe_actions_by_name([richer_first, poorer_second], "WebAutomation", "web.jar")
    assert "중복 정의 발견" in capsys.readouterr().out


def test_no_duplicate_produces_no_log(capsys):
    _dedupe_actions_by_name([_action("a", 2), _action("b", 3)], "Pkg", "pkg.jar")
    assert capsys.readouterr().out == ""


def test_exact_duplicate_collapses_to_one():
    a = _action("checkelement", 5)
    b = _action("checkelement", 5)
    result = _dedupe_actions_by_name([a, b], "WebAutomation", "web.jar")
    assert len(result) == 1


def test_no_id_collision_after_dedup_across_whole_package():
    # 실측 재현: 50개 중 17개가 중복인 WebAutomation 패턴 — dedup 후 유일 이름 개수만 남아야 한다
    actions = []
    for i in range(33):
        actions.append(_action(f"unique{i}", 3))
    for i in range(17):
        actions.append(_action(f"dup{i}", 4))
        actions.append(_action(f"dup{i}", 6))
    result = _dedupe_actions_by_name(actions, "WebAutomation", "web.jar")
    names = [a["name"] for a in result]
    assert len(names) == len(set(names)) == 50


def _pkg(name, version, action_count=3, source_jar="x.jar"):
    return {
        "package_name": name,
        "package_version": version,
        "source_jar": source_jar,
        "actions": [_action(f"a{i}", 1) for i in range(action_count)],
    }


def test_version_sort_key_orders_semantic_versions_correctly():
    assert _version_sort_key("3.8.0") > _version_sort_key("2.3.0")
    assert _version_sort_key("2.10.0") > _version_sort_key("2.9.0")  # 문자열 비교라면 반대로 틀렸을 케이스
    assert _version_sort_key(None) < _version_sort_key("1.0.0")


def test_version_sort_key_uses_date_suffix_as_tiebreak():
    assert _version_sort_key("2.3.0-20220101-000000") > _version_sort_key("2.3.0-20210101-000000")


def test_select_better_version_picks_higher_version():
    older = _pkg("Number", "2.3.0-20210118-185335", source_jar="Imagine_2021.jar")
    newer = _pkg("Number", "3.8.0", source_jar="Base-Automation-Template.jar")
    result = select_better_version(older, newer)
    assert result["package_version"] == "3.8.0"
    assert result["other_versions_seen"] == [
        {"package_version": "2.3.0-20210118-185335", "source_jar": "Imagine_2021.jar", "action_count": 3},
    ]


def test_select_better_version_same_version_and_source_is_a_noop():
    # 실측: parse-jars를 같은 디렉터리에 재실행하면 기존 packages.json 항목과 방금
    # 다시 파싱한 결과가 완전히 동일해진다 — 이 경우 자기 자신을 other_versions_seen에
    # 중복 기록하면 안 된다.
    a = _pkg("WebAutomation", "4.0.9-20220516-163932", action_count=33, source_jar="web.jar")
    b = _pkg("WebAutomation", "4.0.9-20220516-163932", action_count=33, source_jar="web.jar")
    result = select_better_version(a, b)
    assert result is a
    assert "other_versions_seen" not in result


def test_select_better_version_order_independent():
    older = _pkg("Number", "2.3.0")
    newer = _pkg("Number", "3.8.0")
    assert select_better_version(newer, older)["package_version"] == "3.8.0"
    assert select_better_version(older, newer)["package_version"] == "3.8.0"


def test_select_latest_per_package_across_many_versions():
    # 실측 재현: Number 패키지가 9개 저장소에서 서로 다른 버전으로 발견된 상황
    variants = [_pkg("Number", v) for v in [
        "2.0.0-20200721-222224", "2.1.0-20201014-042823", "2.1.0-20201126-165444",
        "2.0.0-20200721-222224", "3.6.0-20220928-104414", "2.0.0-20200624-041934",
        "2.3.0-20210118-185335", "3.8.0", "2.3.0-20210118-185335",
    ]]
    result = _select_latest_per_package(variants)
    assert len(result) == 1
    assert result[0]["package_version"] == "3.8.0"
    assert len(result[0]["other_versions_seen"]) == 8
