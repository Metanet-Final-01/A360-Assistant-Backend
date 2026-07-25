"""A360 언어 수준 어휘 — 카탈로그 재적재로 거짓이 되지 않는 것만 담는다.

## 왜 버전 밖인가

에이전트 버전 패키지는 완전 벤더링이라 도메인 지식을 각자 들고 있다. 그 결과 카탈로그
표기 세대가 바뀔 때마다 v1·v2·v3가 **따로따로** 썩었고, 실패가 에러가 아니라 침묵으로
나타났다 — `v3/verify/checker.py`의 주석이 그 사례를 기록해 뒀다:

    "Excel_MS/OpenSpreadsheet 등은 카탈로그에 없어 R7/R8이 한 번도 발화하지 못했다"

검사가 통과한 게 아니라 검사가 안 돈 것이다. 지식을 한 곳에 모으면 이 침묵이 한 번에
드러난다. v4부터 이 모듈을 쓰고, v1~v3는 자기 상수를 그대로 둔다(비교 셀렉터 유지).

## 무엇을 담고 무엇을 안 담는가

판별 기준 한 줄: **"카탈로그를 다시 적재하면 이 줄이 거짓이 될 수 있는가?"**

- 담는다 — 제어 흐름 패키지 이름, 역할 판정 규칙, `$var$` 표기. A360 **언어**의 일부라
  카탈로그 적재 방식과 무관하다.
- 안 담는다 — 구체 액션명(`cloudExcelOpen`), 세션 opener/closer 쌍. 이건 `derive.py`가
  카탈로그에서 유도한다.

역할 판정을 정규식·부분 문자열로 두는 것은 타협이 아니라 **표기 세대 3벌(`Try` /
`errorHandlerTry` / `try`)을 전부 견딘 유일하게 성공한 설계**다. 유도로 대체하지 않는다.
"""

import re

# 본문(children)을 가질 수 있는 제어 흐름 패키지. A360에는 임의 병합점이 없어 분기·반복
# 블록이 끝나면 다음 형제로 이어진다 — 본문을 갖는 건 이 패키지들뿐이다.
#
# 패키지 단위로 판정하는 이유(RPA-141): 카탈로그가 llm_agent 소싱으로 바뀌며 액션명이 문서
# 슬러그 기반 camelCase가 됐고(`Loop/cloudUsingLoopAction`), Loop 패키지엔 iterator 변형이
# 20여 개다. (package, action) 열거는 표기가 바뀔 때마다 헛위반을 만든다 — 실제로 구 봇
# JSON 표기(`ErrorHandler/try`)가 전부 불일치해 에이전트가 Loop/If/Try를 올바르게 써도
# 재생성을 유발했다.
CONTAINER_PACKAGES: frozenset[str] = frozenset(
    {"Loop", "If", "Step", "Error handler", "Trigger loop"}
)

# 사람의 응답을 기다리는 대화형 패키지 — 무인 실행(트리거) 흐름에 있으면 봇이 무기한 멈춘다.
# 정규화(공백 제거·소문자) 후 비교한다.
ATTENDED_PACKAGES: frozenset[str] = frozenset({"messagebox", "prompt"})

# 파라미터 값 안의 변수 참조 표기 — A360 언어 문법이라 카탈로그와 무관하다.
VAR_REF_RE = re.compile(r"\$([A-Za-z_][\w-]*)\$")

# 패키지 표시명 꼬리의 "패키지"/"package" — 문서 제목과 카탈로그 표기 사이에서 흔들린다.
# `app/rag/build/doc_action_match.py`의 정규화 규칙과 같은 계열로 유지한다.
_PACKAGE_SUFFIX = re.compile(r"\s*(?:패키지|package)\s*$", re.IGNORECASE)


def normalize_package(name: str | None) -> str:
    """패키지 이름을 비교용 키로 정규화한다 — 꼬리 '패키지/package' 제거 + 공백 제거 + 소문자.

    표시명이 "Excel advanced" / "Excel advanced 패키지" / "excel advanced"로 흔들려도
    같은 키가 되게 한다. 빈 입력은 빈 문자열(호출부가 falsy로 걸러낼 수 있게).
    """
    if not name:
        return ""
    return _PACKAGE_SUFFIX.sub("", name).replace(" ", "").replace("_", "").lower()


def is_container(
    package: str | None,
    action: str | None,
    *,
    non_container: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    """이 액션이 children(본문)을 가질 수 있는 컨테이너인지 — R6 판정 기준.

    `non_container`는 컨테이너 패키지 소속이지만 본문을 갖지 않는 (package, action) 예외다
    (Break·Continue·Throw 같은 제어 신호). **기본값이 빈 집합인 게 핵심**이다 — v3는 이걸
    수기 상수 3쌍으로 들고 있었는데 그 3쌍이 현행 카탈로그에 전부 부재라 `Loop/Break`가
    컨테이너로 오판됐다. 호출부가 `derive.derive_container_exceptions(catalog)` 결과를
    넘겨 카탈로그 실재 액션으로 채우게 한다.
    """
    if package not in CONTAINER_PACKAGES:
        return False
    return (package, action) not in non_container


def if_role(action_name: str | None) -> str:
    """If 패키지 액션의 분기 역할 — 'if' | 'elseif' | 'else'.

    프론트 branchRole과 같은 계열. 표기 세대에 무관하도록 부분 문자열로 판정한다.
    'elseif'를 'else'보다 먼저 본다 — 순서를 뒤집으면 elseIf가 else로 잡혀 분기가 뒤집힌다.
    """
    low = (action_name or "").lower().replace("_", "").replace(" ", "")
    if "elseif" in low:
        return "elseif"
    if "else" in low:
        return "else"
    return "if"


def eh_role(action_name: str | None) -> str:
    """Error handler 패키지 액션의 역할 — 'try' | 'catch' | 'finally' | 'throw' | 'other'.

    v3 checker의 판정에 'throw'를 더했다. Throw는 본문을 갖지 않는 제어 신호라
    `derive_container_exceptions`가 이 값으로 컨테이너 예외를 뽑는다.
    """
    low = (action_name or "").lower()
    for role in ("finally", "catch", "throw", "try"):
        if role in low:
            return role
    return "other"


def loop_signal_role(action_name: str | None) -> str | None:
    """Loop 제어 신호 — 'break' | 'continue', 아니면 None.

    Break·Continue는 Loop 패키지 소속이지만 본문을 갖지 않는다.
    """
    low = (action_name or "").lower()
    if "break" in low:
        return "break"
    if "continue" in low:
        return "continue"
    return None


def is_attended(package: str | None) -> bool:
    """사람 응답을 기다리는 패키지인지 — 무인 실행 흐름에서 R15 경고의 기준."""
    return normalize_package(package) in ATTENDED_PACKAGES
