"""문서 HTML을 압축된 구조화 JSON으로 바꾼다 — 향후 LLM 기반 파싱 Agent가 raw HTML을
직접 읽는 대신(문서마다 토큰을 크게 소모) 이 압축된 표현을 저렴하게 소비하도록 하기
위한 전처리 단계다.

CSS/JS(`<script>`/`<style>`)는 파싱에 의미가 없으므로 버린다. 나머지는 태그 이름,
Agent 판단에 실제로 도움이 됐던 속성(`class`, `data-tocid`, `href` 등 — 이번 세션에서
실측으로 신뢰 근거를 확인한 것들, `app/rag/_investigation_notes/HTML_STRUCTURE_INSIGHTS.md` 참고), 텍스트를
`{tag, attrs, text, children}` 형태로 남긴다. `id`처럼 매 문서마다 값이 달라 반복돼도
의미가 없는 속성은 뺀다. `src`(이미지 base64 데이터 URI 등)도 뺀다 — 실측 확인(2026-07-10):
스크린샷이 많은 문서(Excel 고급 패키지, 216KB)는 91%까지 줄어든다. 텍스트 위주 문서는
JSON 구조 자체의 오버헤드(태그/속성 표기) 때문에 원본과 비슷하거나 살짝 커질 수 있는데,
그래도 CSS/JS/id/이미지 데이터 같은 잡음이 전혀 없어서 향후 Agent가 파싱하기엔 더 낫다.
"""

from bs4 import BeautifulSoup, NavigableString

_DROPPED_TAGS = {"script", "style"}

# 실측으로 신뢰 근거가 확인된 속성만 남긴다(2026-07-10):
# - class: DITA 섹션 role(postreq/prereq/example) 판별에 실제로 쓰임
# - data-tocid: 다른 문서 자신의 menu_id와 정확히 일치하는 100% 확정적 링크 대상
# - href: 외부 링크(ft-external-link) 대상 URL
# - data-ft-warning: xref에 흔히 붙는 속성(단, 진짜/가짜 링크 구분 신호는 아님이 확인됨 —
#   그래도 원본 그대로 남겨서 향후 Agent가 스스로 판단하게 한다)
_KEPT_ATTRS = ("class", "data-tocid", "href", "data-ft-warning")

# 이 태그들은 자기 태그 이름 자체가 구조(표 행/셀, 목록 항목, 섹션 경계 등)라 속성이
# 없어도 노드로 남긴다. 그 외 태그(span/strong/b/i/em/kbd/code 등 순수 텍스트 서식용)는
# 의미있는 속성이 하나도 없으면 그냥 자기 텍스트로 접어버린다 — 실측 확인(2026-07-10):
# 접지 않으면 압축 JSON이 원본 HTML보다 더 커진다(예: Aisera 패키지 문서, 5497자 ->
# 6954자). 서식용 wrapper 하나하나를 다 {"tag":...} 노드로 남길 필요가 없다.
_ALWAYS_STRUCTURAL_TAGS = {
    "table", "thead", "tbody", "tr", "td", "th",
    "ul", "ol", "li", "section", "div", "p",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


def _element_to_node(el) -> dict | str | None:
    if isinstance(el, NavigableString):
        text = str(el).strip()
        return text or None

    if el.name in _DROPPED_TAGS:
        return None

    attrs = {}
    for key in _KEPT_ATTRS:
        value = el.get(key)
        if value:
            attrs[key] = " ".join(value) if isinstance(value, list) else value

    children = [child for c in el.children if (child := _element_to_node(c)) is not None]
    # 자식이 전부 이미 텍스트로 접혀 있어야만(dict로 남은 자식이 하나도 없어야) 이
    # 노드도 텍스트로 접을 수 있다 — 자식 중 하나라도 구조(dict)로 남았다면 그건
    # 보존해야 할 진짜 구조이므로, 이 노드도 구조 노드로 유지해서 부모-자식 관계를
    # 잃지 않는다.
    all_children_collapsed = all(isinstance(c, str) for c in children)

    if not attrs and el.name not in _ALWAYS_STRUCTURAL_TAGS and all_children_collapsed:
        # 의미있는 속성도 없고 구조적으로도 안 중요한 태그(span 등 서식용) -> 텍스트로 접기
        text = " ".join(children).strip()
        return text or None

    node: dict = {"tag": el.name}
    if attrs:
        node["attrs"] = attrs
    if len(children) == 1 and isinstance(children[0], str):
        node["text"] = children[0]
    elif children:
        node["children"] = children
    return node


def html_to_structured_json(html: str) -> dict | None:
    """HTML -> {"tag", "attrs", "children"|"text"} 압축 트리. `<div class="body ...">`가
    있으면 그 안쪽만(머리글/네비게이션 등 본문 아닌 부분 제외), 없으면 문서 전체를 쓴다.

    파싱 실패는 크롤링을 막으면 안 되므로 예외를 던지지 않고 None을 반환한다.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - 파싱 실패는 크롤링을 막지 않는다
        return None

    body = soup.find("div", class_="body")
    root = body if body is not None else soup
    return _element_to_node(root)
