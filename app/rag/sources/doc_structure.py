"""docs.automationanywhere.com(Fluid Topics) 토픽 HTML에서 DITA 구조를 추출한다.

원본은 DITA 기반 토픽이라 일관된 시맨틱 구조를 갖는다:
  <p class="shortdesc">한 줄 액션 설명</p>
  <section class="section"><h2 class="title sectiontitle">설정</h2>
    <ul class="ul"><li>설정 항목 1<div class="note">캐비앗</div></li> ... </ul>
  </section>
  <table>...</table>  (모든 페이지에 있진 않음 — 예시 데이터 표이거나, DB 서버 지원
                        매트릭스처럼 그 자체로 의미 있는 참조표인 경우가 있다)

패키지 개요 페이지("{X} 패키지" 제목)는 위 표 중 하나가 특별한 형태다 — 1번째 셀이
<span class="xref ft-internal-link" data-tocid="...">라벨</span>로, 그 패키지의 액션
하위 문서로 가는 진짜 링크다. data-tocid는 그 하위 문서 자신의 toc_id와 정확히
일치한다(실측 확인 완료) — 그래서 "이 문서가 어느 패키지의 몇 번째 액션인지"를 breadcrumb
텍스트 부분일치(오탐 있었음 — Database/DataTable 한국어·영어 불일치 버그)가 아니라
data-tocid 정확 일치로 100% 확정할 수 있다.

docs_crawler.py::html_to_text()는 이 구조를 텍스트로 뭉개 버리는데, action_schema를
문서 기반 LLM 검증으로 채우려면(app/rag/build/doc_schema_llm_verify.py) "설정" 섹션의 <li> 하나가 파라미터/
단계 하나라는 경계 정보와, 표가 있다면 그 표의 행/열 구조(+ 색인 표라면 링크 대상)가
필요하다. 이 모듈은 그 구조만 뽑아 별도 필드로 보존한다.

파싱 실패는 크롤링 자체를 막으면 안 되므로, 어떤 경우에도 예외를 던지지 않고
빈 값의 dict를 반환한다.
"""

from bs4 import BeautifulSoup


def _extract_tables(soup: BeautifulSoup) -> list[dict]:
    tables = []
    for table_el in soup.find_all("table"):
        rows = []
        for tr_el in table_el.find_all("tr"):
            cells = [c.get_text(separator=" ", strip=True) for c in tr_el.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if rows:
            # 첫 행이 <th>로만 구성되면 헤더로 취급하고, 아니면 헤더 없이 전부 데이터 행으로 둔다
            has_header = bool(table_el.find("tr") and table_el.find("tr").find("th"))
            tables.append({
                "headers": rows[0] if has_header else [],
                "rows": rows[1:] if has_header else rows,
            })
    return tables


# DITA 섹션 role. 코퍼스 전수 조사(2026-07-10, 155개 문서의 실제 raw HTML 직접 확인,
# 필터링 전 모든 색인 후보의 조상 section class 조합을 남김없이 셈)로 실제 등장한
# 조합만 담았다 — 여기 없는 이름은 이 코퍼스에서 한 번도 관측되지 않았으므로 추측으로
# 추가하지 않는다:
# - <section class="section postreq">: "데이터베이스에 연결 작업 사용" 문서에서 순환참조
#   (최상위 패키지로 되돌아감)와 전혀 무관한 액션("읽어오기 작업 사용")으로 새는 링크가
#   여기 있었다.
# - <section class="section prereq">: "캡처 작업 이용" 문서에서 같은 패턴으로 확인됨.
# - <section class="example">: "실행 작업 사용" 문서에서 "이 작업을 이용하는 예:"라는
#   문구 뒤에 다른 액션 2개를 예시로 링크하는 목록이 있었다 — 진짜 하위 액션이 아니라
#   사용 예시 인용이다.
# 반면 진짜 색인표는 평범한 <section class="section">/섹션 밖에 있었고, <section
# class="section context">(도입부 설명)에는 실제로 진짜 하위 액션이 들어있는 경우도
# 확인돼(Microsoft Outlook(macOS) 루프 반복자 문서) 제외 대상에서 뺐다 — "관련 항목류
# role이면 무조건 제외"가 아니라 실제 내용을 보고 판단한다.
_NON_PRIMARY_SECTION_ROLES = {"postreq", "prereq", "example"}


def _ancestor_section_classes(el) -> set[str]:
    """el의 조상 중 가장 가까운 <section>의 class 토큰 집합. 조상 section이 없으면 빈 집합."""
    section = el.find_parent("section")
    if section is None:
        return set()
    return set((section.get("class") or []))


def _is_excluded_from_index(el) -> bool:
    """이 후보(tr/ul)가 진짜 하위 액션 색인이 아니라 부가 참고 링크로 판단되면 True.

    두 가지 경로로 제외된다:
    1. 조상 section이 postreq/prereq/example role인 경우 (`_NON_PRIMARY_SECTION_ROLES`).
    2. 조상에 `<div class="note ...">`가 있는 경우 — 실측 확인(2026-07-10, Excel 고급
       패키지): "예제 태스크:"라는 note 안에 다른 액션 2개를 예시로 링크한 목록이 진짜
       카테고리 5개와 나란히 action_index에 섞여 들어간 사례가 있었다. note는 정의상
       본문에 대한 부가 캐비앗이지 새 하위 항목을 선언하는 자리가 아니므로, note 안에
       있다는 사실 자체가 제외 근거로 충분하다(어떤 note 하위 class인지는 안 가린다).
    """
    if _ancestor_section_classes(el) & _NON_PRIMARY_SECTION_ROLES:
        return True
    if el.find_parent("div", class_="note") is not None:
        return True
    return False


def _extract_action_index(soup: BeautifulSoup) -> list[dict]:
    """패키지 개요 페이지의 액션 색인에서 {label, description, target_toc_id}를 뽑는다.

    실측 확인한 결과 패키지마다 마크업이 세 가지로 다르다:
    - 표A(Snowflake/Keynote형): 1번째 셀(작업명) 자체가 <span class="xref" data-tocid>
    - 표B(Python형): 1번째 셀은 평문("닫기"), 2번째 셀(설명)의 "X 작업 항목을
      참조하십시오" 안에 링크가 있음
    - 목록형(Aisera/ServiceNow형): 표가 아예 없고 <ul><li><strong>라벨</strong>: 설명
      <span class="xref" data-tocid>...</span> 항목을 참조하십시오.</li></ul>

    그래서 표는 행(tr) 전체에서 xref를 찾아 label은 항상 1번째 셀 평문으로 통일하고,
    표가 없는 목록형은 <li> 안의 <strong>을 label로 쓴다. data-tocid 있는 xref가 아예
    없는 표/목록(예시 데이터 표, DB 지원 매트릭스, 그냥 설명 문단 등)은 색인이 아니므로
    결과에서 빠진다. 같은 target_toc_id가 여러 마크업에 걸쳐 중복 나오면 한 번만 남긴다.

    `postreq`/`prereq`류 섹션(관련 항목/다음 할 일) 안의 항목은 진짜 하위 액션이 아니라
    "관련 항목" 참고 링크일 수 있으므로(실측 확인됨, `_NON_PRIMARY_SECTION_ROLES` 참고)
    제외한다 — 링크 비율 휴리스틱만으로는 이 둘을 구분할 수 없었다.
    """
    entries = []
    seen_toc_ids: set[str] = set()

    for table_el in soup.find_all("table"):
        for tr_el in table_el.find_all("tr"):
            cells = tr_el.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            xref = tr_el.find("span", class_="xref")
            toc_id = xref.get("data-tocid") if xref else None
            if not toc_id or toc_id in seen_toc_ids:
                continue
            if _is_excluded_from_index(tr_el):
                continue
            seen_toc_ids.add(toc_id)
            entries.append({
                "label": cells[0].get_text(strip=True),
                "description": cells[1].get_text(" ", strip=True),
                "target_toc_id": toc_id,
            })

    for ul_el in soup.find_all("ul"):
        li_els = ul_el.find_all("li", recursive=False)
        qualifying = []
        for li_el in li_els:
            xref = li_el.find("span", class_="xref")
            toc_id = xref.get("data-tocid") if xref else None
            if toc_id:
                qualifying.append((li_el, xref, toc_id))
        # 이 <ul>의 대다수 항목이 xref 링크여야 색인 목록으로 본다 — 액션 상세 페이지의
        # "관련 항목" 목록에 링크 한두 개만 섞여 있는 경우와 구분하기 위한 최소 기준
        # (실측: Aisera 2/2, ServiceNow 트리거 목록 등은 전원이 링크라 통과).
        if len(qualifying) < 2 or len(qualifying) < len(li_els) * 0.6:
            continue
        if _is_excluded_from_index(ul_el):
            continue
        for li_el, xref, toc_id in qualifying:
            if toc_id in seen_toc_ids:
                continue
            seen_toc_ids.add(toc_id)
            strong = li_el.find("strong")
            entries.append({
                "label": strong.get_text(strip=True) if strong else xref.get_text(strip=True),
                "description": li_el.get_text(" ", strip=True),
                "target_toc_id": toc_id,
            })

    return entries


def extract_doc_structure(html: str) -> dict:
    """DITA 토픽 HTML ->
    {"shortdesc": str|None,
     "sections": [{"heading", "items": [{"text", "notes"}]}],
     "tables": [{"headers": [str], "rows": [[str]]}],
     "action_index": [{"label", "description", "target_toc_id"}]}.

    action_index는 패키지 개요 페이지에서만 채워진다 — 그 외 페이지는 빈 리스트.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - 파싱 실패는 크롤링을 막지 않는다
        return {"shortdesc": None, "sections": [], "tables": [], "action_index": []}

    shortdesc_el = soup.find("p", class_="shortdesc")
    shortdesc = shortdesc_el.get_text(separator=" ", strip=True) if shortdesc_el else None

    tables = _extract_tables(soup)
    action_index = _extract_action_index(soup)

    sections = []
    for section_el in soup.find_all("section", class_="section"):
        heading_el = section_el.find("h2", class_="sectiontitle")
        heading = heading_el.get_text(strip=True) if heading_el else ""

        items = []
        for li_el in section_el.find_all("li"):
            note_els = li_el.find_all("div", class_="note")
            notes = [n.get_text(separator=" ", strip=True) for n in note_els]
            for n in note_els:
                n.extract()  # 본문 텍스트에서 note 내용을 분리해 중복 없이 뽑아낸다
            text = li_el.get_text(separator=" ", strip=True)
            if text:
                items.append({"text": text, "notes": notes})

        if heading or items:
            sections.append({"heading": heading, "items": items})

    return {"shortdesc": shortdesc, "sections": sections, "tables": tables, "action_index": action_index}
