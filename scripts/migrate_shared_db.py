"""공유 앱 DB에 Alembic 마이그레이션을 **명시적으로** 적용한다 (RPA-186).

앱 기동 시 도는 `run_migrations()`는 공유 DB(APP_DATABASE_URL)를 건너뛴다 — 팀원이 자기
브랜치로 서버를 띄우는 것만으로 공유 스키마가 그 브랜치 head로 올라가는 걸 막기 위해서다.
그 대신 **`dev`에 머지된 뒤 한 명이** 이 스크립트로 올린다.

    python scripts/migrate_shared_db.py          # 무엇이 적용될지 보여주고 멈춘다(dry-run)
    python scripts/migrate_shared_db.py --apply  # 실제 적용

왜 dry-run이 기본인가: 공유 DB는 되돌리기 어렵다(팀원 데이터가 위에 쌓인다). 무엇이 적용될지
눈으로 확인하고 나서 손으로 --apply를 붙이게 한다.

**--apply는 `origin/dev`에 머지된 커밋에서만 된다 — 코드가 막는다** (CONVENTIONS §7 ①).
브랜치를 출력만 하고 "dev가 아니면 멈추세요"라고 부탁했었는데, 그건 이 PR이 자동 마이그레이션을
코드로 막은 이유와 정면으로 배치된다(규약은 지켜지지 않는다). dry-run은 어느 브랜치에서든 된다.

⚠️ 적용 후 팀에 **공지**한다 — 다른 사람은 pull 해야 코드와 스키마가 맞는다. (이건 코드가
   못 막는 부분이다 — 사람이 해야 한다.)
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# `python scripts/migrate_shared_db.py`로 부르면 sys.path[0]이 scripts/라 app을 못 찾는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _current_revision(engine) -> str | None:
    """DB의 현재 Alembic 리비전. **신규 DB면 None** (예외 아님).

    ⚠️ `select ... from alembic_version`을 그냥 던지면 안 된다 — 테이블 자체가 없는 신규 DB에서
    예외가 난다. 공유 DB를 처음 붙이는 순간이 이 스크립트가 가장 필요한 때인데 거기서 크래시하면
    쓸모가 없다 (CodeRabbit #258).
    """
    from sqlalchemy import inspect, text

    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as c:
        return c.execute(text("select version_num from alembic_version")).scalar_one_or_none()


def _pending_revisions(script, current: str | None) -> list[str]:
    """`current` 이후로 적용될 리비전을 **적용 순서(아래→위)**로 돌려준다.

    ⚠️ `walk_revisions(base="base")`로 전부 훑고 current만 빼면 **이미 적용된 과거 리비전까지**
    '대기'로 뜬다 (CodeRabbit #258). base를 현재 리비전으로 잡아야 한다.
    """
    if current is None:  # 신규 DB — 전부 적용 대상
        revs = list(script.walk_revisions())
    else:
        revs = [s for s in script.walk_revisions(base=current, head="heads")
                if s.revision != current]
    return [s.revision for s in reversed(revs)]  # walk는 head→base 순서라 뒤집는다


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10,
                          cwd=str(Path(__file__).resolve().parent.parent))


def _current_branch() -> str:
    """체크아웃된 git 브랜치 — 실패해도 스크립트를 막지 않는다(정보 제공용)."""
    try:
        r = _git("rev-parse", "--abbrev-ref", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else "(불명)"
    except Exception:  # noqa: BLE001 — git이 없거나 저장소 밖
        return "(불명)"


def _head_is_in_dev() -> bool | None:
    """지금 체크아웃한 커밋이 `origin/dev`에 **포함**돼 있나. 판정 불가면 None.

    브랜치 **이름**이 'dev'인지 보는 건 대리 지표다. 진짜 불변식은 *"올리려는 리비전이 이미
    dev에 머지됐나"*이고, 그건 `HEAD`가 `origin/dev`의 조상인지로 정확히 답한다. 이 편이
    이름 비교보다 낫다:
      - feature 브랜치 → 조상 아님 → 차단 ✓
      - 로컬 dev인데 아직 push 안 한 커밋이 있음 → 조상 아님 → **차단** ✓ (이름 비교는 통과시킨다)
      - detached HEAD인데 dev에 들어있는 커밋 → 조상 → 허용 ✓ (이름 비교는 막는다)
    """
    try:
        if _git("rev-parse", "--verify", "--quiet", "origin/dev").returncode != 0:
            return None  # origin/dev를 모른다 (fetch 안 함 / 리모트 없음)
        return _git("merge-base", "--is-ancestor", "HEAD", "origin/dev").returncode == 0
    except Exception:  # noqa: BLE001 — git 없음·저장소 밖·타임아웃
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="공유 앱 DB에 Alembic 마이그레이션 적용")
    parser.add_argument("--apply", action="store_true",
                        help="실제로 적용한다 (기본은 무엇이 적용될지 보여주고 멈춤)")
    parser.add_argument("--i-know-dev-check-is-broken", action="store_true",
                        help="origin/dev 확인을 건너뛴다 — git이 없는 환경 등 예외용. "
                             "머지 안 된 리비전을 올리면 팀 스키마가 깨진다.")
    args = parser.parse_args()

    import app.db as app_db  # load_dotenv()가 여기서 돈다 — .env의 APP_DATABASE_URL을 읽으려면 필요
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not os.getenv("APP_DATABASE_URL", "").strip():
        print("APP_DATABASE_URL이 설정돼 있지 않습니다 — 공유 DB가 아닙니다.\n"
              "로컬 DB는 앱 기동 시 자동으로 마이그레이션됩니다. 이 스크립트는 불필요합니다.",
              file=sys.stderr)
        return 1

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    script = ScriptDirectory.from_config(cfg)

    # head가 둘이면 get_current_head()가 예외를 던진다 — 이 스크립트가 막으려는 바로 그 상황인데
    # 거기서 스택트레이스만 뱉으면 아무도 원인을 모른다. 사람이 읽을 수 있게 알려준다.
    heads = script.get_heads()
    if len(heads) > 1:
        print(f"리비전 head가 {len(heads)}개입니다: {', '.join(heads)}\n"
              "두 브랜치가 각자 마이그레이션을 만들었습니다 — 병합(alembic merge)이 먼저입니다.\n"
              "이 상태로는 적용할 수 없습니다 (CONVENTIONS §7).", file=sys.stderr)
        return 1
    head = heads[0]

    # engine은 app.db가 이미 APP_DATABASE_URL로 만들어 뒀다.
    current = _current_revision(app_db.engine)

    print(f"대상 DB   : {app_db.engine.url.host}/{app_db.engine.url.database}")
    print(f"git 브랜치: {_current_branch()}   (--apply는 origin/dev에 머지된 커밋에서만)")
    print(f"현재 리비전: {current or '(없음 — 신규 DB)'}")
    print(f"코드 head : {head}")

    if current == head:
        print("\n이미 최신입니다 — 적용할 게 없습니다.")
        return 0

    pending = _pending_revisions(script, current)
    print(f"\n적용 대기: {len(pending)}개 (아래→위 순서로 적용)")
    for rev in pending:
        print(f"  - {rev}")

    if not args.apply:
        print("\n[dry-run] 실제로 적용하려면 --apply 를 붙이세요.\n"
              "⚠️ 공유 DB입니다 — 팀 전체가 영향을 받고 되돌리기 어렵습니다.\n"
              "   적용 후 팀에 공지하세요.")
        return 0

    # --apply 게이트 — dev에 머지된 리비전만 올린다 (CONVENTIONS §7 ①).
    # 브랜치를 **출력만 하고 사람에게 멈추라고 부탁**하면 안 된다 — 이 PR이 자동 마이그레이션을
    # 코드로 막은 이유와 같다(규약은 지켜지지 않는다). dry-run은 열어두고 적용만 막는다.
    if not args.i_know_dev_check_is_broken:
        in_dev = _head_is_in_dev()
        if in_dev is None:
            print("\n`origin/dev`를 확인할 수 없어 적용을 중단합니다 (git 없음 / fetch 안 됨?).\n"
                  "  git fetch origin dev\n"
                  "그래도 안 되면 --i-know-dev-check-is-broken 으로 넘길 수 있습니다.",
                  file=sys.stderr)
            return 1
        if not in_dev:
            print(f"\n지금 커밋이 origin/dev에 없습니다 (브랜치: {_current_branch()}).\n"
                  "**머지되지 않은 리비전을 공유 DB에 올리면 팀 스키마가 그 브랜치 상태로 갑니다** —\n"
                  "리뷰에서 까여도 되돌리기 어렵고, 다른 사람은 그 컬럼을 모릅니다 (CONVENTIONS §7).\n"
                  "  git fetch origin dev && git checkout dev && git pull\n"
                  "먼저 PR을 dev에 머지하세요. (dry-run은 어느 브랜치에서든 됩니다.)",
                  file=sys.stderr)
            return 1

    print("\n적용 중...")
    app_db.run_migrations(allow_shared=True)  # 이 스크립트만 가드를 넘는다
    print("완료. 팀에 공지하세요 — 다른 사람은 pull 해야 코드와 스키마가 맞습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
