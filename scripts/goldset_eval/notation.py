"""표기 정규화 매처 — 골드셋(봇 JSON 표기)과 KB/에이전트 출력(문서 슬러그 표기)을 잇는다.

두 표기 체계가 근본적으로 다르다:
- 골드셋(A360 원본 봇): packageName="Excel_MS", commandName="GetMultipleCells" / "restPost"
- 현재 KB(공식문서 적재): package_name="Excel advanced",
  action_name="excelAdvancedPackageGetMultipleCellsAction" / (POST는 미적재)

전략: 패키지는 별칭 표로 канон 키에 사상하고, 액션은 camelCase 토큰화 후
노이즈 토큰(package/action/cloud/using…)과 패키지 반향 토큰(excel/advanced…)을 걷어낸
의미 토큰 집합으로 비교한다. 불규칙 표기(loop.commands.start ↔ cloudUsingLoopAction)는
소수의 수동 별칭으로 처리한다.

유사도: 토큰 집합 동일=1.0 / 한쪽 포함=0.85 / 그 외 Jaccard. MATCH_THRESHOLD 이상이면 동치.
"""

import re

MATCH_THRESHOLD = 0.55

# ── 패키지 별칭 → 정준 키 ────────────────────────────────────────────────────
# 좌변은 소문자·[_/.-]→공백 정규화 후의 이름. 우변이 정준 키.
_PKG_ALIASES = {
    "excel ms": "excel_adv",
    "excel advanced": "excel_adv",
    # 기본 Excel은 신 KB에 없다. 업무 흐름 동치성 관점에선 고급 Excel과 같은 단계라
    # 채점에선 excel_adv로 접는다(OpenSpreadsheet↔cloudExcelOpen 등이 자연 매칭).
    "excel": "excel_adv",
    "microsoft 365 excel package in automation 360": "excel_365",
    "microsoft 365 excel": "excel_365",
    "office365excel": "excel_365",
    "message box": "messagebox",
    "messagebox": "messagebox",
    "error handler": "errorhandler",
    "errorhandler": "errorhandler",
    "rest": "rest",
    "rest web services": "rest",
    "logtofile": "logging",
    "logging": "logging",
    "data table": "datatable",
    "datatable": "datatable",
    "csv/txt": "csvtxt",
    "csv txt": "csvtxt",
    "csv": "csvtxt",
    "mswordpackage": "word",
    "ms word": "word",
    "word": "word",
    "task bot": "taskbot",
    "taskbot": "taskbot",
    "terminal emulator": "terminal",
    "text file": "textfile",
    "json utilities": "json",
    "jsonhandler": "json",
    "html parser": "html",
    "htmlparser": "html",
    "trigger loop": "triggerloop",
    "web automation": "webautomation",
    "webautomation": "webautomation",
    "credential manager": "credential",
    "simulate keystrokes": "keystrokes",
}

# 정준 키별 패키지 반향 토큰 — 액션 토큰에서 걷어낸다 (양쪽 표기의 파생 어휘 포함)
_PKG_ECHO = {
    "excel_adv": {"excel", "advanced", "ms", "adv", "spreadsheet", "spread", "sheet", "workbook"},
    "excel_365": {"office365", "excel", "ms365", "microsoft", "365", "spreadsheet", "spread", "sheet", "workbook"},
    "messagebox": {"message", "box"},
    "errorhandler": {"error", "handler"},
    "rest": {"rest", "web", "services"},
    "logging": {"log", "logging"},
    "datatable": {"data", "table", "datatable"},
    "word": {"msword", "word"},
    "taskbot": {"task", "bot", "taskbot"},
    "email": {"email", "mail"},
    "xml": {"xml"},
    "string": {"string"},
    "number": {"number"},
    "loop": {"loop"},
    "step": {"step"},
    "if": {"if"},
    "browser": {"browser"},
    "file": {"file"},
    "folder": {"folder"},
    "pdf": {"pdf"},
    "boolean": {"boolean"},
    "datetime": {"datetime"},
    "dictionary": {"dictionary"},
    "list": {"list"},
    "window": {"window"},
    "database": {"database"},
    "system": {"system"},
    "gmail": {"gmail"},
    "csvtxt": {"csv", "txt", "text"},
}

# 토큰화 시 무시하는 무의미 토큰 (표기 체계의 접두/접미 장식)
_NOISE = {
    "package", "action", "actions", "cloud", "using", "the", "a", "an",
    "command", "commands", "pkg", "act", "in", "of", "csh",
}

# 불규칙 표기 수동 별칭 — (pkg_key, 정규화 액션명) → 의미 토큰 집합
_ACTION_ALIASES: dict[tuple[str, str], frozenset] = {
    # Loop 본체: 골드 loop.commands.start ↔ KB cloudUsingLoopAction
    ("loop", "loop commands start"): frozenset({"loop"}),
    ("loop", "cloudusingloopaction"): frozenset({"loop"}),
    ("loop", "start"): frozenset({"loop"}),
    # Step 구획
    ("step", "step"): frozenset({"step"}),
    ("step", "stepaction"): frozenset({"step"}),
    # 이메일 세션 종료: closeEmail ↔ emailDisconnectAction
    ("email", "closeemail"): frozenset({"disconnect"}),
    # If 본체 (KB에는 else/elseif만 있음 — if 본체는 gap으로 드러난다)
    ("if", "if"): frozenset({"if"}),
    # MessageBox (v2 문서 정본 표기 "Message box"까지 — 2026-07-19 v2 카탈로그 평가 대비)
    ("messagebox", "messagebox"): frozenset({"show"}),
    ("messagebox", "message box"): frozenset({"show"}),
    ("messagebox", "usingmessageboxaction"): frozenset({"show"}),
    # 브라우저 열기 계열: launchWebsite/openbrowser ↔ browserPackageOpenAction
    ("browser", "launchwebsite"): frozenset({"open"}),
    ("browser", "openbrowser"): frozenset({"open"}),
    # DLL 실행: RunCSharpDLL_V1 ↔ usingRunFunctionAction
    ("dll", "runcsharpdll v1"): frozenset({"run", "function"}),
    # (구)JSONHandler ↔ (신)JSON utilities
    ("json", "query"): frozenset({"get", "node"}),
    ("json", "initialize"): frozenset({"start", "session"}),
}

# 검수기(v3 checker.CONTAINER_PACKAGES)가 KB 스키마 없이도 허용하는 컨테이너 패키지 —
# KB에 액션 스키마가 없어도 에이전트가 출력할 수 있으므로 '달성 가능'으로 취급한다.
CONTAINER_PKG_KEYS = frozenset({"loop", "if", "step", "errorhandler", "triggerloop"})


# ── 기능 등가 도메인 (RPA-298) ───────────────────────────────────────────────
#
# **왜 필요한가.** 엄격 채점은 `pkg_key`가 다르면 유사도를 0으로 하드 게이트한다. 그래서
# 정답 봇이 `Email/emailConnect`로 한 일을 에이전트가 `Microsoft 365 Outlook/Connect`로
# 하면 0점이다. 실측(케이스 01, v4): n_pred 19 · n_matched 3인데 불일치 16건 중 대부분이
# 이런 **대안 경로**였다 — 틀린 게 아니라 다른 패키지로 같은 일을 했다.
#
# 우리가 정한 정답 기준은 "비전문가가 흐름도를 보고 Control Room에 손으로 넣으면
# **돌아간다**"이다. 그 기준에서는 두 경로가 모두 성공이다. 엄격 채점은 그걸 못 잰다.
#
# **묶는 기준: 같은 자원을 조작하는가.** 사람이 손으로 옮길 때 서로 바꿔도 그 업무 단계가
# 성립해야만 같은 도메인이다. 아래는 의도적으로 **묶지 않은** 것들이다:
#   - `OCR` ↔ `Recorder` — 픽셀 판독과 UI 객체 제어는 결과가 다르다.
#   - `Google Sheets` ↔ `Excel advanced` — 로컬 .xlsx 경로를 못 연다.
#   - `Python Script` ↔ `VBScript` ↔ `DLL` — "외부 코드 실행"으로 묶으면 사람이 옮길 게
#     완전히 달라진다.
#   - `Rest` ↔ 전용 패키지(`Jira` 등) — REST는 **범용 전송**이라 도메인이 없다. 정답의
#     호출 대상(URL)을 봐야 하므로 이 표로는 못 푼다(별도 항목).
#
# ⚠️ **이 표는 정답셋과 카탈로그만 보고 썼다 — 에이전트 출력은 보지 않았다.** 예측을 보고
#    등가를 늘리면 답안지를 예측에 맞춰 고쳐 쓰는 것(순환)이 된다. 표를 늘릴 때도 같은
#    규율을 지킬 것. 실제로 어떤 등가가 발동했는지는 `equiv_pairs`로 매 채점에 남으므로
#    사후 감사가 가능하다.
#
# 여기 없는 패키지는 도메인이 곧 자기 자신이라 엄격 채점과 동작이 같다.
_PKG_DOMAIN = {
    # 메일 — 같은 사서함에 붙어 읽고 보낸다
    "email": "mail", "gmail": "mail", "microsoft365outlook": "mail",
    "microsoftoutlook(macos)": "mail", "applemail": "mail",
    # 스프레드시트 — 같은 통합 문서를 연다 (클라우드 백엔드가 다른 Google Sheets·
    # Apple Numbers는 제외: 로컬 경로를 못 연다)
    "excel_adv": "spreadsheet", "excelbasic": "spreadsheet", "excel_365": "spreadsheet",
    # 워드 문서
    "word": "worddoc", "applepages": "worddoc",
    # 프레젠테이션
    "powerpoint": "slides", "applekeynote": "slides",
    # 클라우드 파일 저장소 — 원격 폴더에 올리고 내린다
    "googledrive": "cloudfiles", "microsoft365onedrive": "cloudfiles",
    "box": "cloudfiles", "sharepoint": "cloudfiles",
    # 브라우저 구동 — 같은 페이지를 열고 요소를 다룬다
    "browser": "webauto", "webautomation": "webauto",
    # 팀 채팅
    "slack": "chat", "microsoftteams": "chat",
    # 평문/구분자 텍스트 파일
    "csvtxt": "textfiles", "textfile": "textfiles",
    # 일정
    "googlecalendar": "calendar", "microsoft365calendar": "calendar",
    "applecalendar": "calendar",
}


def capability_domain(pkg_key: str) -> str:
    """패키지 정준 키 → 기능 도메인. 표에 없으면 자기 자신(= 엄격 채점과 동일)."""
    return _PKG_DOMAIN.get(pkg_key, pkg_key)


def is_scaffold(package: str, action: str) -> bool:
    """순수 구획(Step)·주석(Comment) 여부 — 골드/예측 양쪽에서 액션 지표 제외 대상.

    골드 쪽은 gold.SCAFFOLD가 원표기로 거르지만, 예측 쪽은 표기가 자유로워
    (Step/stepAction, Step/step …) 정준 키로 판별한다.
    """
    return canon_package(package) in ("step", "comment")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _stem(tok: str) -> str:
    """경량 어간 정규화 — 표기 체계 간 형태 차이(deleteFiles↔DeletingFile)를 접는다.

    ing 제거 → 복수 s 제거 → 말음 e 제거. 완전한 형태소 분석이 아니라 두 표기가 같은
    어간으로 접히기만 하면 된다(delete→delet, deleting→delet, files→fil, file→fil).
    """
    t = tok
    if len(t) > 5 and t.endswith("ing"):
        t = t[:-3]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    if len(t) > 3 and t.endswith("e"):
        t = t[:-1]
    return t


def _norm_name(name: str) -> str:
    """소문자화 + 구분자([_/.-])를 공백으로 + 공백 축약."""
    s = re.sub(r"[_/\.\-]+", " ", (name or "").strip())
    return re.sub(r"\s+", " ", s).lower()


def canon_package(raw: str) -> str:
    n = _norm_name(raw)
    if n in _PKG_ALIASES:
        return _PKG_ALIASES[n]
    return n.replace(" ", "")


def _tokenize(name: str) -> list[str]:
    """camelCase·구분자 분해 → 소문자 토큰."""
    parts: list[str] = []
    for chunk in re.split(r"[^0-9A-Za-z]+", name or ""):
        if not chunk:
            continue
        parts.extend(t.lower() for t in _CAMEL_RE.split(chunk) if t)
    return parts


def action_tokens(pkg_key: str, raw_pkg: str, raw_action: str) -> frozenset:
    """액션의 의미 토큰 집합. 수동 별칭 → 토큰화(노이즈·패키지 반향 제거) 순.

    반향 제거로 공집합이 되면 제거 전 집합으로 폴백한다(stepAction→{step} 류) —
    '패키지명 자체가 곧 액션'인 단일 액션 패키지를 살리기 위함.
    """
    alias = _ACTION_ALIASES.get((pkg_key, _norm_name(raw_action)))
    if alias is not None:
        return frozenset(_stem(t) for t in alias)
    toks = [t for t in _tokenize(raw_action) if t not in _NOISE]
    echo = _PKG_ECHO.get(pkg_key, set(_tokenize(raw_pkg)))
    stripped = [t for t in toks if t not in echo]
    return frozenset(_stem(t) for t in (stripped or toks))


def similarity(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a <= b or b <= a:
        return 0.85
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


class CanonAction:
    """비교 가능한 정규화 액션 — (패키지 정준 키, 의미 토큰 집합, 원 표기)."""

    __slots__ = ("pkg_key", "domain", "tokens", "raw")

    def __init__(self, package: str, action: str):
        self.pkg_key = canon_package(package)
        self.domain = capability_domain(self.pkg_key)
        self.tokens = action_tokens(self.pkg_key, package, action)
        self.raw = (package, action)

    def sim(self, other: "CanonAction") -> float:
        """엄격 유사도 — 패키지가 다르면 0. 기존 기준선 채점이 이걸 쓴다."""
        if self.pkg_key != other.pkg_key:
            return 0.0
        return similarity(self.tokens, other.tokens)

    def equiv_sim(self, other: "CanonAction") -> float:
        """기능 등가 유사도 — 패키지 대신 **자원 도메인**으로 게이트한다 (RPA-298).

        도메인이 표에 없는 패키지는 도메인 = 자기 자신이라 `sim`과 결과가 같다.
        즉 이 축은 엄격 채점을 **완화만** 하며, 새로 맞는 쌍만 생긴다(단조).

        토큰 유사도는 그대로 쓴다 — 도메인이 같아도 하는 일이 다르면
        (`mail/Connect` vs `mail/Send`) 토큰이 안 겹쳐 여전히 안 맞는다.
        """
        if self.domain != other.domain:
            return 0.0
        return similarity(self.tokens, other.tokens)

    def __repr__(self) -> str:  # 디버그 가독성
        return f"{self.raw[0]}/{self.raw[1]}→{self.pkg_key}:{{{','.join(sorted(self.tokens))}}}"
