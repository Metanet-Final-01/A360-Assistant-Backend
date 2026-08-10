"""사용자 제공 카탈로그 → 구조화 (규칙 파서, LLM 없음). RPA-285.

## 왜 필요한가

`extract_user_catalog`는 대화 전체를 LLM에 넘겨 액션 목록을 JSON으로 **재출력**시킨다.
그 방식은 카탈로그가 커지면 출력 토큰 한도에서 무너진다 — 실측:

    액션 22개 → 출력 1,628토큰 ✅      액션 47개 → 2,556 ✅      액션 53개 → 4,929 ✅
    액션 448개 → 4만 토큰 필요 → 모델이 796토큰에서 포기, **0개 반환**

액션당 약 90토큰이 정직하게 든다. 규모가 곧 벽이다.

이 모듈은 그 앞에 서서, **형식이 규칙적인 카탈로그를 LLM 없이 구조화**한다. 448개짜리
마크다운 카탈로그가 0콜로 통과하면 출력 한도 문제 자체가 사라진다. 형식을 못 알아보면
`None`을 돌려주고 호출부가 LLM 경로로 넘긴다 — 규칙은 **빠른 길이지 유일한 길이 아니다**.

## 설계 원칙: 애매하면 포기한다

부분 파싱은 최악이다. 448개 중 40개만 긁고 성공했다고 하면, 나머지 408개는 "카탈로그에 없는
액션"이 되어 R1이 무더기로 터지고 사용자는 왜인지 알 수 없다. 그래서 수용 기준을 두고
(`_ACCEPT_RATIO`), 목록처럼 생긴 줄을 충분히 못 읽으면 통째로 `None`을 낸다.

## 읽는 형식

    ## Excel                                     ← 그룹(패키지)
    - **Launch Excel** — 설명 / 필수: 파일 경로(File)*   ← 라벨·설명·파라미터
    - Launch Excel (Document path, Sheet name)          ← 괄호형 파라미터
    - Excel/Launch Excel                                ← 패키지/액션 직접 표기
    | **열기** (`OpenSpreadsheet`) | 설명 | 파일 경로(FILE*) | ...  ← 마크다운 표

표기 보존이 계약이다(`other_catalog.md`: "표기가 한 글자라도 바뀌면 안 됨") — 흐름도에 그대로
쓰이고 R1이 그 문자열로 조회한다. 그래서 라벨을 다듬거나 대소문자를 정규화하지 않는다.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# 목록처럼 생긴 줄 중 이 비율 이상을 액션으로 읽어야 결과를 채택한다. 못 미치면 형식을
# 잘못 짚은 것이므로 통째로 포기하고 LLM에 넘긴다 — 반쪽 카탈로그가 가장 나쁘다.
_ACCEPT_RATIO = 0.6
_MIN_ACTIONS = 3

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*•·]\s+(.+?)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
# 코드펜스 안은 예제일 수 있어 건너뛴다
_FENCE = re.compile(r"^\s*```")

# 그룹 제목 꼬리 정리: "Excel (41)", "Excel 고급 (`Excel_MS`) — 액션 53개"
_GROUP_TAIL = re.compile(r"\s*[(（][^)）]*[)）]\s*$|\s*[—–-]\s*액션\s*\d+\s*개\s*$")
# 파라미터 한 덩이: "이름(타입*)" / "이름{선택1/선택2}*" / "이름"
_PARAM = re.compile(r"^(?P<name>[^(){}]+?)\s*(?:[({](?P<spec>[^)}]*)[)}])?\s*(?P<star>\*)?$")


def _clean_group(title: str) -> str:
    t = title.strip()
    t = re.sub(r"`", "", t)
    prev = None
    while prev != t:  # "Excel (41) — 액션 41개" 처럼 꼬리가 겹칠 수 있다
        prev = t
        t = _GROUP_TAIL.sub("", t).strip()
    return t


def _split_params(blob: str) -> list[dict]:
    """"a(TEXT*), b{A/B}, c" → 파라미터 스펙 목록. 이름만 있으면 이름만 담는다."""
    out: list[dict] = []
    depth = 0
    buf: list[str] = []
    chunks: list[str] = []
    for ch in blob:
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            chunks.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    chunks.append("".join(buf))

    for raw in chunks:
        piece = raw.strip()
        if not piece:
            continue
        m = _PARAM.match(piece)
        if not m:
            continue
        name = m.group("name").strip().strip("*").strip()
        if not name:
            continue
        spec = (m.group("spec") or "").strip()
        required = bool(m.group("star")) or spec.endswith("*")
        spec = spec.rstrip("*").strip()
        param: dict = {"name": name, "required": required}
        if spec:
            # 선택지("A/B/C")와 타입 이름을 가른다 — 슬래시가 있으면 선택지로 본다
            if "/" in spec:
                param["options"] = [{"label": o.strip(), "value": o.strip()} for o in spec.split("/") if o.strip()]
            else:
                param["type"] = spec
        out.append(param)
    return out


def _parse_item(text: str, group: str) -> dict | None:
    """항목 한 줄 → {package, action, label, parameters}. 액션명을 못 찾으면 None."""
    body = text.strip()
    if not body:
        return None

    # 파라미터 꼬리 분리: "… / 필수: a, b" 또는 "… (a, b, c)"
    params: list[dict] = []
    req_split = re.split(r"\s*/\s*(?:필수|required)\s*[:：]\s*", body, maxsplit=1, flags=re.IGNORECASE)
    if len(req_split) == 2:
        body, params = req_split[0].strip(), _split_params(req_split[1])

    # 설명 꼬리 제거: "이름 — 설명" / "이름 - 설명" / "이름: 설명"
    head = re.split(r"\s+[—–]\s+|\s+-\s+|\s*[:：]\s+", body, maxsplit=1)[0].strip()

    # 괄호형 파라미터: "Launch Excel (Document path, Sheet name)"
    if not params:
        m = re.match(r"^(?P<name>.+?)\s*[(（](?P<ps>[^)）]*)[)）]\s*$", head)
        if m and "," in m.group("ps"):
            head = m.group("name").strip()
            params = _split_params(m.group("ps"))

    # 강조·백틱 제거 후 내부 표기(`actionId`)가 있으면 액션 id로 쓴다
    internal = None
    id_m = re.search(r"[(（]\s*`([^`]+)`\s*[)）]", head)
    if id_m:
        internal = id_m.group(1).strip()
        head = head[: id_m.start()].strip()
    label = re.sub(r"\*\*|`", "", head).strip().strip(".").strip()
    if not label:
        return None

    # "패키지/액션" 직접 표기
    package = group
    action = internal or label
    if "/" in label and not internal:
        left, _, right = label.partition("/")
        if left.strip() and right.strip():
            package, action = left.strip(), right.strip()
            label = action

    if not package or not action:
        return None
    spec: dict = {"package": package, "action": action, "label": label}
    # ⚠️ 파라미터를 못 읽었으면 **키 자체를 넣지 않는다**. `[]`는 "파라미터 없음 확정"이라
    #    체커가 R2로 "스펙에 없는 파라미터"를 잡아버린다(_check_parameters 주석: 빈 목록은
    #    확정, None은 모름 → 침묵). 카탈로그가 필수 인자만 적어 준 경우가 흔해서, 못 읽은
    #    것을 '없음'으로 단정하면 멀쩡한 흐름도가 위반투성이가 된다.
    #    `None`을 넣어도 안 된다 — `_menu_block`의 `spec.get("parameters", [])`가 None을
    #    그대로 돌려줘 순회에서 터진다. 키 부재라야 양쪽이 의도대로 동작한다.
    if params:
        spec["parameters"] = params
    return spec


# 액션 표의 첫 열 이름. **부분일치로 보면 안 된다** — 그룹 목록 표의 "액션 수" 열이 걸려
# 그룹 행까지 액션으로 읽힌다(실측: 448개 카탈로그가 501개로 부풀었다).
_ACTION_COLUMNS = frozenset({"액션", "액션명", "action", "action name", "actions"})


def _is_action_table(cols: list[str]) -> bool:
    """이 표가 **액션 표**인가 — 개요·그룹 목록 표를 액션으로 오인하지 않게.

    카탈로그 문서에는 액션 표 말고도 표가 있다(요약 통계, 그룹별 액션 수 목록). 첫 칸만 보고
    읽으면 그것들까지 액션이 된다.
    """
    return any(c.strip() in _ACTION_COLUMNS for c in cols)


def _from_json(text: str) -> list[dict] | None:
    """카탈로그가 통째로 JSON 배열/객체로 주어진 경우."""
    start = text.find("[")
    if start == -1:
        return None
    try:
        data = json.loads(text[start : text.rfind("]") + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    out: list[dict] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        pkg = str(row.get("package") or row.get("group") or "default").strip()
        act = str(row.get("action") or row.get("name") or "").strip()
        if not act:
            continue
        params = row.get("parameters") or row.get("params") or []
        out.append({
            "package": pkg, "action": act, "label": str(row.get("label") or act),
            "parameters": [p for p in params if isinstance(p, dict)],
        })
    return out or None


def parse_catalog(text: str) -> list[dict] | None:
    """규칙으로 카탈로그를 구조화한다. 형식을 못 알아보면 None.

    반환 형태는 `UserCatalogAction.as_spec()`과 같은 dict 목록이라 `UserCatalog`이 그대로 받는다.
    """
    if not text or not text.strip():
        return None

    as_json = _from_json(text)
    if as_json and len(as_json) >= _MIN_ACTIONS:
        logger.info("카탈로그 규칙 파싱(JSON) — 액션 %d개", len(as_json))
        return as_json

    group = "default"
    candidates = 0   # 목록처럼 생긴 줄
    parsed: list[dict] = []
    seen: set[tuple[str, str]] = set()
    in_fence = False
    table_cols: list[str] | None = None

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        h = _HEADING.match(line)
        if h:
            cleaned = _clean_group(h.group(1))
            if cleaned:
                group = cleaned
            table_cols = None
            continue

        row = _TABLE_ROW.match(line)
        if row:
            if _TABLE_SEP.match(line):
                continue
            cells = [c.strip() for c in row.group(1).split("|")]
            if table_cols is None:  # 첫 행은 헤더
                table_cols = [c.lower() for c in cells]
                continue
            if not _is_action_table(table_cols):
                # 개요·그룹 목록 같은 표는 액션 표가 아니다. 후보로도 세지 않는다 —
                # 세면 수용 비율이 엉뚱하게 떨어져 멀쩡한 카탈로그를 포기하게 된다.
                continue
            candidates += 1
            # 액션 열은 첫 칸, 파라미터 열이 있으면 그 칸을 함께 읽는다
            item = cells[0]
            pidx = next((i for i, c in enumerate(table_cols) if "파라미터" in c or "param" in c), None)
            if pidx is not None and pidx < len(cells) and cells[pidx] not in ("", "-"):
                item = f"{item} / 필수: {cells[pidx]}"
            got = _parse_item(item, group)
        else:
            b = _BULLET.match(line)
            if not b:
                continue
            candidates += 1
            got = _parse_item(b.group(1), group)

        if got and (got["package"], got["action"]) not in seen:
            seen.add((got["package"], got["action"]))
            parsed.append(got)

    if len(parsed) < _MIN_ACTIONS or not candidates:
        return None
    ratio = len(parsed) / candidates
    if ratio < _ACCEPT_RATIO:
        logger.info("카탈로그 규칙 파싱 포기 — 목록 줄 %d개 중 %d개만 읽음(%.0f%%)",
                    candidates, len(parsed), ratio * 100)
        return None
    logger.info("카탈로그 규칙 파싱 — 액션 %d개 (목록 줄 %d개)", len(parsed), candidates)
    return parsed
