"""`.env`의 `APP_DATABASE_URL`을 켜고 끈다 (RPA-186).

    python scripts/toggle_shared_app_db.py           # 현재 상태만 표시
    python scripts/toggle_shared_app_db.py --off     # 로컬로 (스키마 작업 시작할 때)
    python scripts/toggle_shared_app_db.py --on      # 공유로 (평소·작업 끝난 뒤)

왜 스크립트인가: 값이 크레덴셜이라 **읽어서 화면에 띄우면 안 된다**. 이 스크립트는 줄 앞에
'#'만 붙였다 뗐다 하고 상태(켜짐/꺼짐)만 출력한다 — URL 자체는 절대 출력하지 않는다.

왜 토글이 필요한가 (docs/CONVENTIONS.md §7):
  공유 DB에선 앱 기동 시 자동 마이그레이션이 **꺼진다**(팀원이 서버 띄우는 것만으로 공유
  스키마가 그 브랜치 head로 올라가는 걸 막기 위해). 그래서 새 리비전을 만드는 중에 토글이
  켜져 있으면 **로컬에도 공유에도 적용이 안 돼** 앱이 쿼리에서 "column does not exist"로
  터진다. 스키마 작업 중엔 로컬로 두고, dev 머지 후 `migrate_shared_db.py --apply`로 올린다.
"""

import argparse
import io
import re
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
KEY = "APP_DATABASE_URL"
LINE = re.compile(rf"^(#\s*)?({KEY}=)(.*)$", re.M)


def main() -> int:
    parser = argparse.ArgumentParser(description=f".env의 {KEY} 토글 (값은 출력하지 않음)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="공유 DB로 (주석 해제)")
    g.add_argument("--off", action="store_true", help="로컬 DB로 (주석 처리)")
    args = parser.parse_args()

    if not ENV.exists():
        print(f".env가 없습니다: {ENV}", file=sys.stderr)
        return 1

    # ⚠️ newline="" 가 **읽기에도** 필요하다. 기본(universal newlines)으로 읽으면 CRLF가 \n으로
    #    번역되고, 그대로 쓰면 파일 **전체**가 LF로 바뀐다 — 한 줄만 토글하려다 6,420바이트를
    #    통째로 고쳐놓는다. (실측으로 잡음: --off/--on 왕복 후 해시가 달라졌다.)
    #    이 스크립트는 사용자의 .env(크레덴셜 보관 파일)를 건드리므로, 지정한 한 줄 외에는
    #    **한 바이트도** 바뀌면 안 된다.
    text = io.open(ENV, encoding="utf-8", newline="").read()
    m = LINE.search(text)
    if not m:
        print(f".env에 {KEY} 줄이 없습니다 — 이미 로컬입니다(추가하려면 .env.example 참고).")
        return 0

    commented = m.group(1) is not None
    if not (args.on or args.off):  # 조회만
        print(f"{KEY}: {'꺼짐 → 로컬 DB' if commented else '켜짐 → 공유 Neon'}")
        return 0

    want_on = args.on
    if want_on == (not commented):
        print(f"이미 {'켜짐(공유)' if want_on else '꺼짐(로컬)'} 상태입니다 — 변경 없음.")
        return 0

    new_line = f"{m.group(2)}{m.group(3)}" if want_on else f"#{m.group(2)}{m.group(3)}"
    io.open(ENV, "w", encoding="utf-8", newline="").write(
        text[:m.start()] + new_line + text[m.end():])

    print(f"{KEY}: {'꺼짐 → 켜짐 (공유 Neon)' if want_on else '켜짐 → 꺼짐 (로컬 DB)'}")
    if want_on:
        print("  ⚠️ 공유 DB는 자동 마이그레이션이 꺼집니다. dev 머지 후 스키마를 올리려면:\n"
              "     python scripts/migrate_shared_db.py --apply")
    else:
        print("  로컬 docker가 떠 있어야 합니다: docker compose up -d db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
