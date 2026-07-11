"""app/rag/build/merge.py 단위 테스트 — 순수 파이썬 조립 로직, LLM/DB 불필요.

JAR 경로(schema_source="jar")가 기존과 동일하게 동작하는지, 그리고 리프=액션
베이스라인(naive_leaf_actions)이 action_candidate로만 순수 추가되고 JAR 커버 패키지는
절대 안 건드리는지를 확인한다. 리프의 진짜 액션 여부/파라미터 스키마 추출은 팀
결정(2026-07-10)으로 별도의 LLM 기반 파싱 Agent가 담당할 예정이며, merge.py는 그
산출물 포맷이 확정되기 전까지 이 경로(action_schema)를 갖지 않는다.
"""

import pytest

from app.rag.build.merge import build_rag_documents

_JAR_PACKAGE = {
    "package_name": "Excel_MS",
    "package_label": "Excel 고급",
    "package_version": "5.3.0",
    "package_description": "엑셀 자동화",
    "actions": [{
        "name": "getWorksheetAsDataTable",
        "label": "워크시트를 데이터 테이블로 가져오기",
        "description": "지정된 워크시트에서 데이터 테이블을 생성합니다.",
        "parameters": [{"name": "sheetSelection", "type": "RADIO", "required": True,
                         "options": [{"label": "ActiveWorksheet", "value": "ActiveWorksheet"}]}],
        "return_type": "TABLE",
        "return_label": None,
    }],
}


def _build(packages=None, naive_leaf_actions=None):
    return build_rag_documents(
        packages=packages if packages is not None else [_JAR_PACKAGE],
        docs=[], locale="ko-KR", bots=None, naive_leaf_actions=naive_leaf_actions, chunk_size=None,
    )


def test_jar_action_schema_tagged_with_schema_source_jar():
    docs = _build()
    action_rows = [d for d in docs if d["source_type"] == "action_schema"]
    assert len(action_rows) == 1
    assert action_rows[0]["metadata"]["schema_source"] == "jar"
    assert action_rows[0]["metadata"]["schema"] == _JAR_PACKAGE["actions"][0]


def test_duplicate_action_ids_raise_instead_of_silently_overwriting():
    # 서로 다른 두 액션이 같은 (package_name, action_name)으로 귀결되면 id가 겹쳐서
    # DB의 ON CONFLICT(id) upsert가 하나를 조용히 지워버린다 — 조용히 넘어가지 않고
    # build 단계에서 바로 터뜨려야 한다. 정본 판단(JAR 자체에 중복 액션이 있을 때 어느
    # 버전을 쓸지)은 jar_parser.py가 파싱 시점에 이미 처리하므로, 여기 도달할 때 겹치면
    # 그건 예상 못 한 새로운 데이터 문제다.
    colliding_package = {
        **_JAR_PACKAGE,
        "actions": [
            {"name": "captureImagebyPath", "label": "경로로 캡처", "description": "d",
             "parameters": [], "return_type": None, "return_label": None},
            {"name": "captureImagebyPath", "label": "URL로 캡처", "description": "d",
             "parameters": [], "return_type": None, "return_label": None},
        ],
    }
    with pytest.raises(ValueError, match="중복 id"):
        _build(packages=[colliding_package])


def test_naive_leaf_action_becomes_action_candidate_not_action_schema():
    naive = [{"package_name": "Database", "title": "연결 문서", "url": "https://docs/connect",
              "path_titles": ["Database 패키지", "연결 문서"]}]
    docs = _build(naive_leaf_actions=naive)
    db_rows = [d for d in docs if d["package_name"] == "Database"]
    assert len(db_rows) == 1
    assert db_rows[0]["source_type"] == "action_candidate"
    assert db_rows[0]["action_name"] is None
    assert db_rows[0]["url"] == "https://docs/connect"
    assert db_rows[0]["metadata"]["schema_source"] == "naive_leaf_action"
    assert db_rows[0]["metadata"]["path_titles"] == ["Database 패키지", "연결 문서"]


def test_naive_leaf_action_never_overrides_jar_covered_package():
    naive = [{"package_name": "Excel_MS", "title": "이런 게 있었으면 안 됨",
              "url": "https://docs/x", "path_titles": []}]
    docs = _build(naive_leaf_actions=naive)
    assert all(d.get("title") != "Excel_MS - 이런 게 있었으면 안 됨" for d in docs)
    action_rows = [d for d in docs if d["package_name"] == "Excel_MS" and d["source_type"] == "action_schema"]
    assert len(action_rows) == 1
