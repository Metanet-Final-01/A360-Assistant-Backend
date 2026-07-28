"""버전 밖 도메인 지식층 — 에이전트 버전들이 공유하는 A360 지식 (RPA-298).

버전 패키지(`app/agent/v1`…)는 완전 벤더링이라 도메인 지식을 각자 복사해 들고 있었고,
카탈로그 표기 세대가 바뀔 때마다 따로따로 썩었다. 그 실패는 에러가 아니라 **침묵**으로
나타난다(검사가 통과한 게 아니라 안 도는 것). 이 패키지는 그 지식을 한 곳에 모은다.

**v3가 이 패키지를 쓴다.** v1·v2는 얼린 기준선이라 자기 상수를 그대로 둔다 — 어휘가
같이 움직이면 버전 간 비교가 무의미해진다.

(처음에는 v4 전용이었다. v4 폐기 후 v3가 수기 어휘를 이쪽으로 넘겼다 —
`v3/recommend/research.py`의 `_STRUCTURAL_CANDIDATES` 주석이 예고해 둔 이관이다.)

폴더 이름이 `^v\\d+$`에 걸리지 않으므로 `registry._discover()`가 이걸 에이전트 버전으로
오인하지 않는다(`v0`·`v10` 같은 이름은 절대 쓰지 말 것).

## 구성

- `lexicon` — 카탈로그 재적재로 거짓이 되지 않는 A360 언어 수준 어휘(제어 흐름 패키지,
  역할 판정, `$var$` 표기). 리터럴로 유지한다.
- (후속) `derive` — 카탈로그에서 유도하는 어휘(세션 opener/closer 등). 수기 상수는 폴백.
- (후속) `contract` — 적재 계약 미충족 시 '우연한 침묵'을 '명시적 침묵'으로.
- (후속) `channels` — 단계별 검색 채널 정의.
- (후속) `examples` — 공식 문서 자동화 용례 자산.

## 설계 제약

이 패키지는 **catalog·retriever를 인자로 받고 자체 팩토리를 두지 않는다.** 그래서
`tests/conftest.py`의 스텁 대상이 아니고, DB 소유권을 서비스 계층에 두는 INTERFACES
계약도 위반하지 않는다. 설정이 필요하면 `app.core.config` 경유 — `os.getenv` 직접 호출은
`tests/test_config_registry.py`의 래칫에 걸린다.
"""

from .lexicon import (
    ATTENDED_PACKAGES,
    CONTAINER_PACKAGES,
    VAR_REF_RE,
    eh_role,
    if_role,
    is_attended,
    is_container,
    loop_signal_role,
    normalize_package,
)

__all__ = [
    "ATTENDED_PACKAGES",
    "CONTAINER_PACKAGES",
    "VAR_REF_RE",
    "eh_role",
    "if_role",
    "is_attended",
    "is_container",
    "loop_signal_role",
    "normalize_package",
]
