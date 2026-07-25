"""v4 설정 — 값은 `app.core.config` 레지스트리에서 읽는다.

v1~v3의 config.py는 `os.getenv`를 직접 부르고 `tests/test_config_registry.py`의
`_DIRECT_GETENV_ALLOWED` 화이트리스트에 등재돼 있다. 그 래칫은 "빼는 변경은 환영,
넣는 변경은 위반"이므로 신규인 v4는 처음부터 레지스트리를 경유한다. 세 키는 이미
`app/core/config.py`의 REGISTRY에 EnvSpec으로 선언돼 있어 추가 선언이 필요 없다.

값을 모듈 `__getattr__`로 노출해 **접근 시점**에 평가한다. v3는 import 시점 스냅샷이라
테스트가 env를 바꿔도 이미 읽은 값이 남았는데, 여기서는 즉시 반영된다.

소비 방식은 v3와 동일하다 — `from .. import config` 후 `config.OPENAI_MODEL`.
"""

from pathlib import Path

# 루트 .env를 방어적으로 로드한다. app/db.py·app/main.py도 로드하지만, 에이전트만
# 단독 import하는 경로(평가 스크립트 등)에서는 그것들이 안 걸릴 수 있다. v3가 같은
# 이유로 하던 일을 유지한다 — os.getenv 호출이 아니라 래칫과 무관하다.
try:
    from dotenv import load_dotenv

    # parents[3] = 리포 루트 (이 파일은 app/agent/v4/config.py — 버전 폴더로 한 단계 깊다).
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
except ImportError:
    pass

from app.core import config as _core  # noqa: E402 — dotenv 로드가 먼저여야 한다

# 이 모듈이 노출하는 키. REGISTRY에 없는 이름을 적으면 접근 시 core가 AttributeError를 낸다.
_KEYS = frozenset({"OPENAI_API_KEY", "OPENAI_MODEL", "MAX_LLM_CONCURRENCY"})


def __getattr__(name: str):
    """모듈 속성 접근을 core 레지스트리 조회로 연결한다 (PEP 562)."""
    if name in _KEYS:
        return _core.get(name)
    raise AttributeError(f"v4 config가 노출하지 않는 이름: {name}")


def __dir__() -> list[str]:
    return sorted(_KEYS)
