"""공유 앱 DB에 Alembic 마이그레이션을 **명시적으로** 적용한다 (RPA-186).

앱 기동 시 도는 `run_migrations()`는 공유 DB(APP_DATABASE_URL)를 건너뛴다 — 팀원이 자기
브랜치로 서버를 띄우는 것만으로 공유 스키마가 그 브랜치 head로 올라가는 걸 막기 위해서다.
그 대신 **`dev`에 머지된 뒤 한 명이** 이 스크립트로 올린다.

    python scripts/migrate_shared_db.py          # 무엇이 적용될지 보여주고 멈춘다(dry-run)
    python scripts/migrate_shared_db.py --apply  # 실제 적용

왜 dry-run이 기본인가: 공유 DB는 되돌리기 어렵다(팀원 데이터가 위에 쌓인다). 무엇이 적용될지
눈으로 확인하고 나서 손으로 --apply를 붙이게 한다.

⚠️ 적용 전 확인 (CONVENTIONS §7):
   1. 지금 체크아웃이 **dev**이고 최신인가? feature 브랜치 head를 올리면 남의 브랜치가 깨진다.
   2. 적용 후 팀에 **공지**한다 — 다른 사람은 pull 해야 코드와 스키마가 맞는다.
"""

import argparse
import os
import sys
from pathlib import Path

# `python scripts/migrate_shared_db.py`로 부르면 sys.path[0]이 scripts/라 app을 못 찾는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _current_branch() -> str:
    """체크아웃된 git 브랜치 — 실패해도 스크립트를 막지 않는다(정보 제공용)."""
    import subprocess

    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=5,
                           cwd=str(Path(__file__).resolve().parent.parent))
        return r.stdout.strip() if r.returncode == 0 else "(불명)"
    except Exception:  # noqa: BLE001 — git이 없거나 저장소 밖
        return "(불명)"


def main() -> int:
    parser = argparse.ArgumentParser(description="공유 앱 DB에 Alembic 마이그레이션 적용")
    parser.add_argument("--apply", action="store_true",
                        help="실제로 적용한다 (기본은 무엇이 적용될지 보여주고 멈춤)")
    args = parser.parse_args()

    import app.db as app_db  # load_dotenv()가 여기서 돈다 — .env의 APP_DATABASE_URL을 읽으려면 필요
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    if not os.getenv("APP_DATABASE_URL", "").strip():
        print("APP_DATABASE_URL이 설정돼 있지 않습니다 — 공유 DB가 아닙니다.\n"
              "로컬 DB는 앱 기동 시 자동으로 마이그레이션됩니다. 이 스크립트는 불필요합니다.",
              file=sys.stderr)
        return 1

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    # 지금 공유 DB가 몇 번인지 — engine은 app.db가 이미 APP_DATABASE_URL로 만들어 뒀다.
    with app_db.engine.connect() as c:
        row = c.execute(text("select version_num from alembic_version")).scalar_one_or_none()
    current = row or "(비어 있음 — 신규 DB)"

    print(f"대상 DB   : {app_db.engine.url.host}/{app_db.engine.url.database}")
    print(f"git 브랜치: {_current_branch()}   ← dev가 아니면 멈추세요 (CONVENTIONS §7)")
    print(f"현재 리비전: {current}")
    print(f"코드 head : {head}")

    if current == head:
        print("\n이미 최신입니다 — 적용할 게 없습니다.")
        return 0

    pending = [s.revision for s in script.walk_revisions(base="base", head="heads")
               if s.revision != current]
    print(f"\n적용 대기: {len(pending)}개 (아래→위 순서로 적용)")
    for rev in reversed(pending):
        print(f"  - {rev}")

    if not args.apply:
        print("\n[dry-run] 실제로 적용하려면 --apply 를 붙이세요.\n"
              "⚠️ 공유 DB입니다 — 팀 전체가 영향을 받고 되돌리기 어렵습니다.\n"
              "   dev 최신인지 확인하고, 적용 후 팀에 공지하세요.")
        return 0

    print("\n적용 중...")
    app_db.run_migrations(allow_shared=True)  # 이 스크립트만 가드를 넘는다
    print("완료. 팀에 공지하세요 — 다른 사람은 pull 해야 코드와 스키마가 맞습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
