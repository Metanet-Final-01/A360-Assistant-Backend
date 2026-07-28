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
    """
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
def _import_version(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def resolve_version(version: str | None):
    """버전 문자열 → 구현 모듈(지연 import). None이면 기본 버전.

    미지 버전(명시 요청)은 ValueError — 엔드포인트가 available_versions()로 사전 검증하지만
    계약을 코드에서도 강제한다. env 기본의 미지값은 default_version()이 이미 폴백 처리한다.
    """
    name = version or default_version()
    if name not in _discover():
        raise ValueError(
            f"알 수 없는 에이전트 버전: {name!r} (사용 가능: {list(_discover())})"
        )
    return _import_version(name)
