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
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# CI 잡 타임아웃보다 짧게 — 우리가 먼저 끝나야 원본을 복원할 수 있다.
_TEST_TIMEOUT_SEC = int(os.getenv("PROVE_TEETH_TIMEOUT_SEC", "300"))


def _run_test(test: str) -> tuple[str, str]:
    """("pass" | "fail" | "timeout", 요약).

    ⚠️ timeout 필수 (CodeRabbit #263). 없으면 pytest가 멈출 때 **상위 CI 타임아웃이 이 프로세스를
       통째로 죽여** 아래 finally의 원본 복원이 안 돈다 — 즉 **결함이 심긴 소스가 그대로 남는다.**
    ⚠️ 타임아웃을 "fail"로 뭉개면 안 된다 (#263 2차): 결함 삽입 후 pytest가 **멈춘** 것을
       "테스트가 결함을 잡았다"로 오판한다. 멈춤은 이빨이 아니라 판정 불가다 — 3상태로 구분한다.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True, text=True, errors="replace", cwd=str(ROOT),
            timeout=_TEST_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return "timeout", f"⏱ {_TEST_TIMEOUT_SEC}초 초과로 중단 (테스트가 멈췄다)"
    tail = [l for l in r.stdout.splitlines() if l.strip()][-1:] or ["(출력 없음)"]
    return ("pass" if r.returncode == 0 else "fail"), tail[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="변형을 심어 테스트가 깨지는지 확인한다")
    ap.add_argument("--file", required=True, help="변형할 소스 파일")
    ap.add_argument("--old", required=True, help="찾을 문자열 (정확히 1번 나와야 한다)")
    ap.add_argument("--new", required=True, help="바꿀 문자열 (= 결함을 심는다)")
    ap.add_argument("--test", required=True, help="pytest 대상 (path 또는 path::test)")
    args = ap.parse_args()

    # ⚠️ 저장소 루트 밖 쓰기 금지 (CodeRabbit #263 3차). `ROOT / 절대경로`는 pathlib에서
    #    **절대경로가 이긴다** — `--file C:/다른곳/x.py`나 `..` 탈출로 저장소 밖 파일을
    #    변조·"복원"할 수 있다. 이 도구는 파일을 썼다 되돌리는 도구라 더 위험하다.
    path = (ROOT / args.file).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        print(f"🔴 저장소 밖 경로는 다루지 않는다: {args.file}", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"파일 없음(또는 일반 파일 아님): {path}", file=sys.stderr)
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
    out_before, sum_before = _run_test(args.test)
    if out_before != "pass":
        print(f"🔴 변형 **전**에 이미 {'멈춘다' if out_before == 'timeout' else '실패한다'}"
              f" — 증명 불가.\n   {sum_before}", file=sys.stderr)
        return 1

    # ② 변형 후: 테스트가 **실패해야** 한다.
    try:
        io.open(path, "w", encoding="utf-8", newline="").write(
            original.replace(args.old, args.new, 1))
        out_after, sum_after = _run_test(args.test)
    finally:
        io.open(path, "w", encoding="utf-8", newline="").write(original)  # 항상 복원

    restored = io.open(path, encoding="utf-8", newline="").read() == original
    if not restored:
        print("🔴 원본 복원 실패! 파일을 직접 확인할 것", file=sys.stderr)
        return 1

    if out_after == "timeout":
        # 멈춤은 "실패"가 아니다 (CodeRabbit #263 2차) — 결함이 무한루프를 만들었을 수도,
        # 환경 문제일 수도 있다. 어느 쪽이든 "테스트가 그 결함을 잡았다"는 증명이 아니다.
        print(f"🔴 **판정 불가** — 변형 후 테스트가 {_TEST_TIMEOUT_SEC}초 안에 끝나지 않았다.\n"
              f"   멈춤 ≠ 이빨. 다른 결함으로 다시 증명할 것. (원본은 복원됨)", file=sys.stderr)
        return 1

    if out_after == "pass":
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
