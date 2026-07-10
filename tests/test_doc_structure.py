"""app/rag/sources/doc_structure.py 단위 테스트.

실제로 재크롤링해 확인한 DITA 구조(shortdesc/section.h2/ul.li/note)와 동일한 형태의
HTML 픽스처로 검증한다 — 네트워크 불필요, 결정론적.
"""

from app.rag.sources.doc_structure import extract_doc_structure

_SAMPLE_HTML = """
<div class="content-locale-ko-KR content-locale-ko"><div id="x"><div class="body conbody">
<p class="shortdesc">데이터 테이블 패키지의 열 삭제 작업에서 특정 열을 삭제할 수 있습니다.</p>
<section class="section"><h2 class="title sectiontitle">설정</h2>
  <ul class="ul" id="x__ul">
    <li class="li" id="x__data_table_name">드롭다운 목록에서 테이블 변수의 이름을 선택합니다.</li>
    <li class="li">삭제할 열 이름 또는 열 인덱스를 지정합니다.
      <div class="note note note_note" id="x__index_count"><span class="note__title">주:</span> 인덱스 카운트는 0부터 시작합니다.</div>
    </li>
  </ul>
</section>
</div></div></div>
"""

_TABLE_HTML = """
<div class="body conbody">
<table><tbody>
  <tr><th>데이터베이스작업</th><th>Microsoft SQL Server</th><th>MySQL</th></tr>
  <tr><td>연결</td><td>Y</td><td>Y</td></tr>
  <tr><td>읽어오기</td><td>Y</td><td>N</td></tr>
</tbody></table>
</div>
"""

# 실측 확인한 패키지 개요 페이지의 색인 표 구조 (Snowflake 패키지 실제 HTML 기준)
_ACTION_INDEX_HTML = """
<div class="body conbody">
<table><tbody>
  <tr><th>작업</th><th>설명</th></tr>
  <tr>
    <td><span class="xref ft-internal-link" data-mapid="T2WjQTA3t9hxmPhJ9~~3~w" data-tocid="1gFklU0mTzH7Lry7gN5AVQ">Snowflake 패키지의 작업 연결</span></td>
    <td><span class="ph">Snowflake</span>에 안전하게 연결하고 자동화 세션을 인증합니다.</td>
  </tr>
  <tr>
    <td><span class="xref ft-internal-link" data-mapid="T2WjQTA3t9hxmPhJ9~~3~w" data-tocid="ABC123">행 선택</span></td>
    <td>select SQL 문을 사용하여 행 세부 정보를 검색합니다.</td>
  </tr>
</tbody></table>
</div>
"""

# 실측 확인한 두 번째 색인 표 형태 (Python 스크립트 패키지 실제 HTML 기준) — 링크가
# 1번째 셀이 아니라 2번째 셀(설명)의 "X 작업 항목을 참조하십시오" 안에 있다.
_ACTION_INDEX_HTML_LINK_IN_DESCRIPTION = """
<div class="body conbody">
<table><tbody>
  <tr><th>작업</th><th>설명</th></tr>
  <tr>
    <td>닫기</td>
    <td><p class="p"><span class="xref ft-internal-link" data-mapid="T2WjQTA3t9hxmPhJ9~~3~w" data-tocid="ddhnC3GqTTHwNH7mOzM9KA">닫기 작업</span> 항목을 참조하십시오.</p></td>
  </tr>
</tbody></table>
</div>
"""

# 실측 확인한 세 번째 형태 (Aisera 패키지 실제 HTML 기준) — 표가 아예 없고 <ul><li> 안에
# <strong>라벨</strong> + 인라인 xref 링크가 있다.
_ACTION_INDEX_HTML_LIST_FORM = """
<div class="body conbody">
<p>이 패키지에는 다음 작업이 포함되어 있습니다.</p>
<ul class="ul" id="x__ul_aisera_actions_overview">
  <li class="li"><strong class="ph b">콘텐츠 수집</strong>: 파일을 업로드합니다.
    <span class="xref ft-internal-link" data-mapid="T2WjQTA3t9hxmPhJ9~~3~w" data-tocid="1UOxPlLTrZ9aBTxQGak39A">콘텐츠 수집 작업</span> 항목을 참조하십시오.</li>
  <li class="li"><strong class="ph b">콘텐츠 질문하기</strong>: 자연어 쿼리를 전송합니다.
    <span class="xref ft-internal-link" data-mapid="T2WjQTA3t9hxmPhJ9~~3~w" data-tocid="XYZ789">콘텐츠 질문하기 작업</span> 항목을 참조하십시오.</li>
</ul>
</div>
"""


def test_extracts_shortdesc():
    result = extract_doc_structure(_SAMPLE_HTML)
    assert result["shortdesc"] == "데이터 테이블 패키지의 열 삭제 작업에서 특정 열을 삭제할 수 있습니다."


def test_extracts_setup_section_with_items():
    result = extract_doc_structure(_SAMPLE_HTML)
    assert len(result["sections"]) == 1
    section = result["sections"][0]
    assert section["heading"] == "설정"
    assert len(section["items"]) == 2


def test_note_nested_under_its_own_item_not_top_level():
    result = extract_doc_structure(_SAMPLE_HTML)
    items = result["sections"][0]["items"]
    assert items[0]["notes"] == []
    assert items[1]["notes"] == ["주: 인덱스 카운트는 0부터 시작합니다."]
    # note 내용이 본문 text에 중복으로 남아있지 않아야 한다
    assert "인덱스 카운트" not in items[1]["text"]


def test_missing_structure_returns_empty_not_raise():
    result = extract_doc_structure("<html><body><p>구조 없는 페이지</p></body></html>")
    assert result == {"shortdesc": None, "sections": [], "tables": [], "action_index": []}


def test_extracts_table_with_header_row():
    result = extract_doc_structure(_TABLE_HTML)
    assert len(result["tables"]) == 1
    table = result["tables"][0]
    assert table["headers"] == ["데이터베이스작업", "Microsoft SQL Server", "MySQL"]
    assert table["rows"] == [["연결", "Y", "Y"], ["읽어오기", "Y", "N"]]


def test_page_without_table_has_empty_tables_list():
    result = extract_doc_structure(_SAMPLE_HTML)
    assert result["tables"] == []


def test_malformed_html_never_raises():
    result = extract_doc_structure("<div><p class=shortdesc>안 닫힌 태그")
    assert isinstance(result, dict)
    assert "shortdesc" in result


def test_extracts_action_index_from_package_overview_table():
    result = extract_doc_structure(_ACTION_INDEX_HTML)
    assert len(result["action_index"]) == 2
    first = result["action_index"][0]
    assert first["label"] == "Snowflake 패키지의 작업 연결"
    assert first["target_toc_id"] == "1gFklU0mTzH7Lry7gN5AVQ"
    assert "인증합니다" in first["description"]


def test_plain_reference_table_has_empty_action_index():
    # data-tocid 링크가 없는 일반 참조표(예: DB 지원 매트릭스)는 색인으로 보지 않는다
    result = extract_doc_structure(_TABLE_HTML)
    assert result["action_index"] == []


def test_action_page_without_index_table_has_empty_action_index():
    result = extract_doc_structure(_SAMPLE_HTML)
    assert result["action_index"] == []


def test_extracts_action_index_when_link_is_in_description_cell():
    # Python 스크립트 패키지형 — 1번째 셀은 평문, 링크는 2번째 셀 안에 있음
    result = extract_doc_structure(_ACTION_INDEX_HTML_LINK_IN_DESCRIPTION)
    assert len(result["action_index"]) == 1
    entry = result["action_index"][0]
    assert entry["label"] == "닫기"  # 1번째 셀의 평문 그대로, xref 텍스트("닫기 작업")가 아님
    assert entry["target_toc_id"] == "ddhnC3GqTTHwNH7mOzM9KA"


def test_extracts_action_index_from_list_form_no_table():
    # Aisera 패키지형 — 표 자체가 없고 <ul><li><strong>+인라인 xref
    result = extract_doc_structure(_ACTION_INDEX_HTML_LIST_FORM)
    assert len(result["action_index"]) == 2
    assert result["action_index"][0]["label"] == "콘텐츠 수집"
    assert result["action_index"][0]["target_toc_id"] == "1UOxPlLTrZ9aBTxQGak39A"
    assert result["action_index"][1]["label"] == "콘텐츠 질문하기"


def test_setup_section_li_without_strong_or_xref_not_treated_as_action_index():
    # _SAMPLE_HTML의 "설정" 섹션 <li>들은 strong/xref가 없으니 action_index로 안 새야 한다
    result = extract_doc_structure(_SAMPLE_HTML)
    assert result["action_index"] == []


def test_extracts_action_index_from_list_without_strong_when_majority_are_links():
    # ServiceNow형 — <strong> 라벨 없이 <li> 전체가 xref 링크뿐인 경우도, 목록 전원이
    # 링크면 색인으로 인정한다(라벨은 xref 자신의 텍스트를 씀).
    html = """
    <ul class="ul">
      <li class="li"><span class="xref" data-tocid="TOC1">ServiceNow 새 기록 트리거 생성하기</span></li>
      <li class="li"><span class="xref" data-tocid="TOC2">ServiceNow 업데이트된 기록 트리거 생성하기</span></li>
    </ul>
    """
    result = extract_doc_structure(html)
    assert len(result["action_index"]) == 2
    assert result["action_index"][0]["label"] == "ServiceNow 새 기록 트리거 생성하기"
    assert result["action_index"][0]["target_toc_id"] == "TOC1"


def test_example_links_inside_note_div_excluded_even_without_special_section_role():
    # 실측 확인(2026-07-10): "Excel 고급 패키지" 문서에서, 진짜 카테고리 5개(<ul> 밖,
    # section 밖) 사이에 "예제 태스크:" note 안의 <ul>에 다른 액션 2개를 예시로 링크한
    # 목록이 섞여 있었다. 이건 section role(postreq/prereq/example)이 아니라 그냥
    # <div class="note note_note"> 안에 있을 뿐이라, section 기준 필터로는 못 걸렀다.
    html = """
    <div class="body conbody">
    <ul class="ul">
      <li class="li"><span class="xref ft-internal-link" data-tocid="CATEGORY_A">셀 작업</span></li>
      <li class="li"><span class="xref ft-internal-link" data-tocid="CATEGORY_B">워크시트 작업</span></li>
    </ul>
    <div class="note note note_note">
      <span class="note__title">예제 태스크:</span>
      <ul class="ul">
        <li class="li"><span class="xref ft-internal-link" data-tocid="EXAMPLE_1">CSV 예제</span></li>
        <li class="li"><span class="xref ft-internal-link" data-tocid="EXAMPLE_2">조건문 예제</span></li>
      </ul>
    </div>
    </div>
    """
    result = extract_doc_structure(html)
    toc_ids = {e["target_toc_id"] for e in result["action_index"]}
    assert toc_ids == {"CATEGORY_A", "CATEGORY_B"}


def test_postreq_section_xref_excluded_even_when_link_ratio_qualifies():
    # 실측 확인(2026-07-10): "데이터베이스에 연결 작업 사용" 문서에서, 진짜 하위 액션
    # (표 안, 평범한 section)과 관련 항목/순환참조(같은 <ul> 안, <section class="section
    # postreq">)가 같은 문서에 섞여 있었다. 링크 비율만으로는 못 걸러서 postreq 섹션
    # 자체를 제외해야 한다.
    html = """
    <div class="body conbody">
    <table><tbody>
      <tr><th>작업</th><th>설명</th></tr>
      <tr>
        <td><span class="xref ft-internal-link" data-tocid="REAL_CHILD">Windows 인증 연결</span></td>
        <td>설명</td>
      </tr>
    </tbody></table>
    <section class="section postreq">
      <ul class="ul">
        <li class="li"><span class="xref ft-internal-link" data-tocid="CYCLE_BACK_TO_ROOT">데이터베이스 패키지</span></li>
        <li class="li"><span class="xref ft-internal-link" data-tocid="UNRELATED_ACTION">읽어오기 작업 사용</span></li>
      </ul>
    </section>
    </div>
    """
    result = extract_doc_structure(html)
    toc_ids = {e["target_toc_id"] for e in result["action_index"]}
    assert toc_ids == {"REAL_CHILD"}


def test_prereq_section_list_also_excluded():
    html = """
    <div class="body conbody">
    <section class="section prereq">
      <ul class="ul">
        <li class="li"><span class="xref" data-tocid="PREREQ1">사전 조건 문서 1</span></li>
        <li class="li"><span class="xref" data-tocid="PREREQ2">사전 조건 문서 2</span></li>
      </ul>
    </section>
    </div>
    """
    result = extract_doc_structure(html)
    assert result["action_index"] == []


def test_example_section_xref_excluded():
    # 실측 확인(2026-07-10): "실행 작업 사용" 문서에서 "이 작업을 이용하는 예:" 뒤에
    # 다른 액션을 예시로 인용하는 목록이 <section class="example"> 안에 있었다 —
    # 진짜 하위 액션이 아니라 사용 예시 인용이므로 제외해야 한다.
    html = """
    <div class="body conbody">
    <section class="example">
      <p class="p">이 작업을 이용하는 예:</p>
      <ul class="ul">
        <li class="li"><span class="xref" data-tocid="EX1">bot 반복</span></li>
        <li class="li"><span class="xref" data-tocid="EX2">오류 처리기</span></li>
      </ul>
    </section>
    </div>
    """
    result = extract_doc_structure(html)
    assert result["action_index"] == []


def test_context_section_with_genuine_actions_not_excluded():
    # 실측 확인(2026-07-10): "Microsoft Outlook(macOS)에 루프 반복자 사용" 문서는
    # <section class="section context"> 안에 진짜 하위 액션(상태 변경/전달/이동 등)이
    # 있었다 — "관련 항목류 role이면 무조건 제외"가 아니라 실제로 확인된 role만
    # 제외해야 한다는 근거.
    html = """
    <div class="body conbody">
    <section class="section context">
      <p class="p">루프 내에서 다음 작업을 이용해야 합니다.</p>
      <ul class="ul">
        <li class="li"><span class="xref" data-tocid="A">상태 변경</span></li>
        <li class="li"><span class="xref" data-tocid="B">전달</span></li>
      </ul>
    </section>
    </div>
    """
    result = extract_doc_structure(html)
    assert {e["target_toc_id"] for e in result["action_index"]} == {"A", "B"}


def test_genuine_index_in_plain_section_not_excluded():
    # 진짜 색인표는 평범한 <section class="section">(postreq/prereq 아님) 안에 있어도
    # 정상적으로 뽑혀야 한다 — 이번 필터가 과도하게 배제하지 않는지 확인.
    html = """
    <div class="body conbody">
    <section class="section">
      <ul class="ul">
        <li class="li"><span class="xref" data-tocid="A">액션 A</span></li>
        <li class="li"><span class="xref" data-tocid="B">액션 B</span></li>
      </ul>
    </section>
    </div>
    """
    result = extract_doc_structure(html)
    assert {e["target_toc_id"] for e in result["action_index"]} == {"A", "B"}


def test_single_stray_related_link_in_prose_list_not_treated_as_index():
    # 액션 상세 페이지의 "관련 항목" 목록처럼, 다른 일반 항목들 사이에 링크 하나만 섞여
    # 있으면(다수가 아니면) 색인으로 오탐하면 안 된다.
    html = """
    <ul class="ul">
      <li class="li">일반 설명 항목 1</li>
      <li class="li">일반 설명 항목 2</li>
      <li class="li">일반 설명 항목 3</li>
      <li class="li">자세한 내용은 <span class="xref" data-tocid="TOC1">관련 문서</span> 참조</li>
    </ul>
    """
    result = extract_doc_structure(html)
    assert result["action_index"] == []
