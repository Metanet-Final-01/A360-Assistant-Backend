"""테스트에 '이빨'이 있는지 기계적으로 증명한다 (RPA-189).

**이빨** = 그 테스트가 실제로 결함을 잡는가. 테스트가 통과하는 이유는 둘이다:
  ① 코드가 옳다  ② **테스트가 아무것도 안 본다**
구분하려면 **일부러 망가뜨려 보고, 테스트가 깨지는지** 봐야 한다. 안 깨지면 그 테스트는 장식이다.

    python scripts/prove_teeth.py \
        --file app/services/alerts.py \
        --old '    if not enabled():' --new '    if False:' \
        --test tests/test_alerts.py::test_disabled_when_webhook_unset

이 스크립트가 존재하는 이유 — 2026-07-16 하루에 **이빨 증명을 3번 헛다리 짚었다**:

  1. `이미 최신` early-return에 걸려 **게이트에 도달조차 안 했는데** "통과했네"로 읽음
  2. `subject` 대입만 try 밖으로 옮기고 **빌더는 안에 남겨** 겨냥한 지점을 안 건드림
  3. 치환 문자열이 CRLF와 안 맞아 **변형이 아예 안 심겼는데** 결과를 그대로 믿음

셋 다 원인이 같다: **변형 후 테스트가 통과하면 "좋다"가 아니라 "증명 실패"인데** 그걸 반대로
읽었다. 그래서 이 스크립트는 그 판정을 사람에게 맡기지 않는다 — **통과 = 에러**로 강제한다.

종료코드: 0 = 이빨 확인(변형 시 테스트 실패) / 1 = 증명 실패(변형이 무효거나 테스트가 장식)
어떤 경우에도 **원본을 복원**한다(에러·중단 포함).
"""

import argparse
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_test(test: str) -> tuple[bool, str]:
    """(통과했나, 요약)."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, errors="replace", cwd=str(ROOT),
    )
    tail = [l for l in r.stdout.splitlines() if l.strip()][-1:] or ["(출력 없음)"]
    return r.returncode == 0, tail[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="변형을 심어 테스트가 깨지는지 확인한다")
    ap.add_argument("--file", required=True, help="변형할 소스 파일")
    ap.add_argument("--old", required=True, help="찾을 문자열 (정확히 1번 나와야 한다)")
    ap.add_argument("--new", required=True, help="바꿀 문자열 (= 결함을 심는다)")
    ap.add_argument("--test", required=True, help="pytest 대상 (path 또는 path::test)")
    args = ap.parse_args()

    path = ROOT / args.file
    if not path.exists():
        print(f"파일 없음: {path}", file=sys.stderr)
        return 1

    # newline="" — 읽기에도 필요하다. 기본(universal newlines)으로 읽으면 CRLF가 \n으로 번역되고,
    # 그대로 쓰면 파일 전체 줄바꿈이 바뀐다(실측으로 .env 6,420바이트를 통째로 고쳐놨다).
    original = io.open(path, encoding="utf-8", newline="").read()

    n = original.count(args.old)
    if n != 1:
        print(f"🔴 --old가 {n}번 나온다 (정확히 1번이어야 한다).\n"
              f"   0번이면 오타·줄바꿈(CRLF) 불일치 — **변형이 안 심긴 채** 결과를 믿게 된다.\n"
              f"   2번 이상이면 어디를 바꾸는지 모른다.", file=sys.stderr)
        return 1

    # ① 변형 전: 테스트가 통과해야 한다. 원래 빨간불이면 이 증명 자체가 무의미하다.
    ok_before, sum_before = _run_test(args.test)
    if not ok_before:
        print(f"🔴 변형 **전**에 이미 실패한다 — 증명 불가.\n   {sum_before}", file=sys.stderr)
        return 1

    # ② 변형 후: 테스트가 **실패해야** 한다.
    try:
        io.open(path, "w", encoding="utf-8", newline="").write(
            original.replace(args.old, args.new, 1))
        ok_after, sum_after = _run_test(args.test)
    finally:
        io.open(path, "w", encoding="utf-8", newline="").write(original)  # 항상 복원

    restored = io.open(path, encoding="utf-8", newline="").read() == original
    if not restored:
        print("🔴 원본 복원 실패! 파일을 직접 확인할 것", file=sys.stderr)
        return 1

    if ok_after:
        print(f"🔴 **증명 실패** — 결함을 심었는데 테스트가 통과했다.\n"
              f"   {sum_after}\n\n"
              f"   둘 중 하나다:\n"
              f"     (a) 이 테스트는 그 결함을 못 잡는다 = 장식이다\n"
              f"     (b) 테스트가 변형한 코드에 **도달하지 못한다**\n"
              f"         (early-return·skip·다른 분기에 걸림 — 실측으로 3번 당한 함정)\n"
              f"   '통과했네'가 아니라 **다시 하라**는 뜻이다.", file=sys.stderr)
        return 1

    print(f"✅ 이빨 확인 — 결함을 심으니 테스트가 깨진다.\n"
          f"   변형 전: {sum_before}\n"
          f"   변형 후: {sum_after}\n"
          f"   → 이 테스트가 그 방어를 실제로 지키고 있다. (원본 복원됨)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
