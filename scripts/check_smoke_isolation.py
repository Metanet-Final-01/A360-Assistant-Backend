"""live smoke 기동 전, 관측 쓰기가 팀 공유 DB로 새지 않는지 확인한다 (RPA-168).

`manage.ps1 smoke`가 기동 직전에 호출한다. 격리를 **바라지 않고 확인**하기 위한 것 —
메커니즘이 깨지면 조용히 오염시키는 대신 시끄럽게 멈춘다(fail-closed).

왜 필요한가: `tests/conftest.py`의 격리 픽스처는 **pytest 전용**이다. `.env` 그대로 uvicorn을
띄우면 관측 미들웨어가 공유 Neon에 그대로 쓴다 — 2026-07-15에 실제로 request_metrics 25행·
audit_logs 22행을 오염시켰다.

검사 2가지 (둘 다 통과해야 LOCAL):
1. OBSERVABILITY_DATABASE_URL이 비어 있다 → 관측 쓰기가 앱 DB로 폴백한다.
2. **그 폴백 대상인 앱 DB도 로컬이다.** 1번만 보면, .env의 DATABASE_HOST가 원격(공유/AWS)을
   가리킬 때 "격리됨"이라 해놓고 그 공유 DB에 관측 데이터를 쓴다 (#234 CodeRabbit 지적).

⚠️ `import app.db`를 반드시 먼저 해야 한다 — load_dotenv()가 거기서만 돈다. observability_db만
import하면 .env가 안 읽혀 **항상 LOCAL이 나오는 가짜 가드**가 된다(실측 2026-07-15에 잡음).

출력: 첫 줄에 LOCAL 또는 SHARED:<사유>. 종료코드는 항상 0 — 판정은 호출자가 한다.
"""

import sys
from pathlib import Path

# `python scripts/check_smoke_isolation.py`로 부르면 sys.path[0]이 scripts/라 app을 못 찾는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def main() -> int:
    """LOCAL 또는 SHARED:<사유>를 찍는다. 종료코드는 항상 0 — 기동 여부는 호출자가 판단한다."""
    import app.db  # noqa: F401 — load_dotenv()가 여기서 돈다. 반드시 먼저.
    from app.core.observability_db import observability_url

    if observability_url():
        print("SHARED:OBSERVABILITY_DATABASE_URL이 설정돼 있습니다 (관측 쓰기가 공유 DB로 갑니다)")
        return 0

    host = (app.db.engine.url.host or "").lower()
    if host not in _LOCAL_HOSTS:
        print(f"SHARED:관측은 앱 DB로 폴백하는데 그 앱 DB가 원격입니다 (DATABASE_HOST={host})")
        return 0

    print("LOCAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
