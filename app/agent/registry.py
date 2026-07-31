"""에이전트 버전 레지스트리 — vN 서브패키지 자동탐색 + 지연 로드.

`app/agent/` 아래 `v1/`·`v2/`… 각 버전 패키지는 완전 벤더링된 독립 에이전트다
(orchestrator·recommend·verify·prompts까지 자기 사본 보유). 이 모듈은 그 버전들을
파일시스템에서 자동 발견하고 — 버전 목록을 코드에 하드코딩하지 않는다, `v3/` 폴더를
두면 자동 인식 — 요청된 버전만 지연 import한다(양 버전을 부팅 때 미리 올리지 않음).

기본 버전은 env `AGENT_VERSION`으로 정한다(없거나 미지값이면 `_FALLBACK_DEFAULT`). 버전을
추가할 때 프론트·백엔드는 손대지 않는다: `GET /api/agent/versions`가 `available_versions()`를
그대로 노출하고, 백엔드 검증도 이 목록으로 동적 수행하기 때문이다(v3 = 폴더 드롭 하나).
"""

import importlib
import importlib.util
import logging
import os
import pkgutil
import re
import sys
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# 버전 패키지 이름 규칙: v + 정수 (registry/__init__ 같은 동료 모듈과 구분).
_VERSION_RE = re.compile(r"^v\d+$")

# env(AGENT_VERSION) 미설정·미지값일 때의 고정 폴백. 새 버전을 '기본'으로 승격하려면
# 코드가 아니라 env를 바꾼다(통제형 거버넌스 — 폴더 추가만으로 기본이 조용히 바뀌지 않게).
_FALLBACK_DEFAULT = "v2"


@lru_cache(maxsize=1)
def _discover() -> tuple[str, ...]:
    """`app/agent/` 아래 vN 서브패키지를 스캔해 버전번호 순 정렬된 id 튜플을 반환한다.

    배포 단위로 폴더가 고정이라 한 번만 스캔한다(lru_cache).
    """
    pkg_path = sys.modules[__package__].__path__  # app.agent 패키지 경로
    names = sorted(
        (
            name
            for _, name, is_pkg in pkgutil.iter_modules(pkg_path)
            if is_pkg and _VERSION_RE.match(name)
        ),
        key=lambda n: int(n[1:]),  # v2 < v10 (문자열 정렬이 아니라 숫자 정렬)
    )
    return tuple(names)


def default_version() -> str:
    """기본 버전 — env `AGENT_VERSION`이 발견된 버전이면 그것, 아니면 폴백."""
    discovered = _discover()
    env = os.getenv("AGENT_VERSION")
    if env in discovered:
        return env
    if env:
        logger.warning("AGENT_VERSION=%r 은 존재하지 않는 버전 — 기본값으로 폴백", env)
    if _FALLBACK_DEFAULT in discovered:
        return _FALLBACK_DEFAULT
    return discovered[-1] if discovered else _FALLBACK_DEFAULT


def _meta_file(version: str) -> Path | None:
    """`vN/meta.py`의 실제 경로 — `_discover()`와 **같은 출처**(패키지 `__path__`)에서 찾는다.

    cwd나 상대경로에 기대면 탐색이 보는 폴더와 메타를 읽는 폴더가 갈릴 수 있다.

    🔴 **이름을 `_discover()`와 같은 규칙(`^v\\d+$`)으로 먼저 검증한다** (Qodo #448). 탐색은 그
    규칙으로 걸러내는데 여기서 안 걸면 둘이 서로 다른 '유효한 버전'을 갖게 된다 — 게다가 이 경로는
    `_meta()`가 `exec_module`로 **실행**하므로, 검증을 건너뛰면 `..`가 든 값이 흘러들었을 때 의도치
    않은 파일을 실행할 수 있다. `v`+숫자만 통과시키면 구분자·상위 이동이 원천 봉쇄된다
    (지금은 호출자가 `_discover()` 결과만 넘기지만, 가드는 호출자의 선의에 기대지 않는다).
    """
    if not _VERSION_RE.match(version):
        return None
    for root in sys.modules[__package__].__path__:
        candidate = Path(root) / version / "meta.py"
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=None)
def _meta(version: str) -> dict:
    """버전의 경량 메타(`vN/meta.py`)만 읽는다 — 전체 에이전트 스택은 import하지 않는다.

    `/api/agent/versions`·FE 셀렉터가 양 버전을 실제로 구동하지 않고 목록만 얻게 한다.
    meta.py가 없거나 깨져도 빈 dict(호출부가 id로 폴백).

    🔴 `importlib.import_module(f"{__package__}.{version}.meta")`를 쓰면 안 된다 (RPA-190).
    파이썬은 하위 모듈을 올리기 전에 **부모 패키지 `app.agent.vN`의 `__init__.py`를 먼저 실행**
    하는데, 그게 analysis·orchestrator·recommend를 끌어온다 → 목록 조회 한 번에 v1·v2·v3의
    전체 스택(실측 72개 모듈)이 로드돼 콜드 컨테이너에서 **7.4초**가 걸렸다(관측 p95 8,094ms의
    정체). meta.py는 dict 리터럴뿐이라 부모 패키지를 거치지 않고 파일에서 단독 실행한다
    (실측 7,423ms → 3ms). 로드한 모듈은 `sys.modules`에 등록하지 않는다 — 등록하면 이름이
    실제 패키지 경로와 섞여 이후 정식 import를 오염시킨다.
    """
    try:
        path = _meta_file(version)
        if path is None:
            return {}
        # 합성 이름(패키지 경로와 무관) — 부모 패키지 import를 유발하지 않는다.
        spec = importlib.util.spec_from_file_location(f"_agent_meta_{version}", path)
        if spec is None or spec.loader is None:
            return {}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        meta = getattr(mod, "VERSION_META", None)
        return dict(meta) if isinstance(meta, dict) else {}
    except Exception:  # noqa: BLE001 — 메타 로드 실패가 목록 조회를 막지 않게
        logger.warning("VERSION_META 로드 실패: %s", version, exc_info=True)
        return {}


def available_versions() -> list[dict]:
    """FE 셀렉터·`GET /api/agent/versions` 용 메타 목록 `[{id,label,description,default}]`."""
    default = default_version()
    return [
        {
            "id": vid,
            "label": _meta(vid).get("label") or vid,
            "description": _meta(vid).get("description") or "",
            "default": vid == default,
        }
        for vid in _discover()
    ]


@lru_cache(maxsize=None)
def import_version(name: str):
    """**이미 검증된** 버전 id → 구현 모듈(지연 import, 캐시).

    이름 해석과 import를 가른 이유: 디스패처는 두 결과가 **다 필요하다**(이름은 done에 새기고,
    모듈은 실행한다). 합쳐진 `resolve_version()`만 있으면 이름을 얻으려 해석을 한 번, 모듈을
    얻으려 또 한 번 — 같은 검증을 두 번 타게 된다 (Qodo #475).

    ⚠️ 검증하지 않는다 — `resolve_version_name()`이 돌려준 값만 넘긴다. 미검증 문자열을 직접
    넘기면 `importlib`가 임의 모듈 경로를 타므로, 외부 입력은 반드시 그쪽을 먼저 통과시킨다.
    """
    return importlib.import_module(f"{__package__}.{name}")


def resolve_version_name(version: str | None) -> str:
    """요청 버전 → **실제로 실행될 버전 id**. None이면 서버 기본 (RPA-184).

    모듈이 아니라 이름을 돌려주는 갈래를 따로 둔 이유: 디스패처가 done 이벤트에 실제 실행
    버전을 새기려면 "무엇을 골랐는지"를 알아야 하는데, 모듈만 받으면 그걸 역으로 알아낼
    방법이 없다(`__name__` 파싱은 계약이 아니라 우연이다).

    미지 버전(명시 요청)은 ValueError — 엔드포인트가 available_versions()로 사전 검증하지만
    계약을 코드에서도 강제한다. **다른 버전으로 조용히 대체하지 않는다**(RPA-184 D-24).

    env 기본(`AGENT_VERSION`)의 미지값만은 default_version()이 경고 로그와 함께 폴백한다 —
    운영자 오설정으로 부팅을 죽이지 않겠다는 기존 결정이다(test_default_version_falls_back_
    for_unknown_env). 그 경우에도 **실행된 버전이 그대로 resolved로 공개**되므로 대체 사실이
    호출자 기록에 남는다 — 조용하지 않다는 계약은 여기서 지켜진다.
    """
    name = version or default_version()
    if name not in _discover():
        raise ValueError(
            f"알 수 없는 에이전트 버전: {name!r} (사용 가능: {list(_discover())})"
        )
    return name


def resolve_version(version: str | None):
    """요청 버전 문자열 → 구현 모듈. None이면 기본 버전 — 해석+import 한 번에 하는 편의 갈래.

    이름이 필요 없는 호출부(`analyze`/`recommend`)용이다. 이름도 함께 필요하면
    `resolve_version_name()` → `import_version()` 두 단계로 나눠 쓴다(중복 검증 방지).
    """
    return import_version(resolve_version_name(version))
