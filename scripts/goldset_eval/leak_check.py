"""업무정의서 구현 어휘 누출 검사 — 골드셋 유지보수 도구.

왜 필요한가: 골드셋 업무정의서는 정답 봇에서 유래하므로, 작성자가 무심코 패키지명·
액션명·파라미터명을 옮겨 적으면 **정답을 보고 문제를 낸 것**이 되어 벤치마크가
무효화된다. 에이전트가 문서에서 액션명을 읽어 그대로 쓰면 재현율이 실력과 무관하게
올라간다.

검사 방식: 각 케이스의 정답 봇 JSON에서 실제로 쓰인 어휘를 긁어 같은 케이스의
업무정의서 본문과 단어 경계로 대조한다. 업무 용어로도 자연스러운 단어(Excel, Email,
File 등)는 ALLOW로 제외해 오탐을 억제한다 — 실제 업무정의서는 "Excel"을 쓸 수밖에 없다.

실행:
    PYTHONUTF8=1 .venv/Scripts/python.exe -m scripts.goldset_eval.leak_check \
        [--goldset "C:\\...\\final-etc-files\\골드셋"]

종료 코드: 누출 0건이면 0, 하나라도 있으면 1 (CI에서 게이트로 쓸 수 있다).
"""

import argparse
import json
import re
from pathlib import Path

# 업무 용어로도 자연스럽게 쓰이는 단어 — 누출로 보지 않는다.
# (실제 업무정의서가 "Excel 파일을 연다"라고 쓰는 것은 정상이다.)
ALLOW = {
    "file", "folder", "email", "excel", "word", "string", "number", "if", "loop",
    "step", "value", "text", "date", "datetime", "url", "browser", "screen",
    "delay", "comment", "table", "list", "dictionary", "record", "window",
    "subject", "message", "session", "output", "input", "count", "type", "name",
    "boolean", "credential", "twilio", "rest", "xml", "dll", "csv", "pdf",
}

# 이보다 짧은 토큰은 우연 일치가 잦아 검사하지 않는다.
_MIN_TERM = 4


def vocab_from_bot(path: Path) -> set[str]:
    """정답 봇 JSON에서 구현 어휘(패키지·액션·파라미터·변수명)를 긁는다."""
    out: set[str] = set()

    def walk(nodes):
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            for k in ("packageName", "commandName"):
                v = n.get(k)
                if isinstance(v, str):
                    out.add(v)
            for a in n.get("attributes") or []:
                nm = a.get("name")
                if isinstance(nm, str):
                    out.add(nm)
            walk(n.get("children"))
            for br in n.get("branches") or []:
                if isinstance(br, dict):
                    v = br.get("commandName")
                    if isinstance(v, str):
                        out.add(v)
                    walk(br.get("children"))

    d = json.loads(path.read_text(encoding="utf-8"))
    walk(d.get("nodes"))
    for v in d.get("variables") or []:
        nm = v.get("name")
        if isinstance(nm, str):
            out.add(nm)
    return out


def check(goldset: Path) -> int:
    """전 케이스를 검사하고 누출 건수를 반환한다."""
    gold_root = goldset / "정답셋"
    docs = goldset / "업무정의서_정규화"
    total = 0

    for case_dir in sorted(gold_root.iterdir()):
        if not case_dir.is_dir():
            continue
        doc = docs / f"{case_dir.name}.md"
        if not doc.exists():
            print(f"[누락] 업무정의서 없음: {case_dir.name}")
            total += 1
            continue

        idx = json.loads((case_dir / "workflow_index.json").read_text(encoding="utf-8"))
        vocab: set[str] = set()
        for wf in idx.get("workflows", []):
            p = case_dir / wf["output_file"]
            if p.exists():
                vocab |= vocab_from_bot(p)

        low = doc.read_text(encoding="utf-8").lower()
        hits = [
            t for t in (v.strip() for v in vocab)
            if len(t) >= _MIN_TERM
            and t.lower() not in ALLOW
            # 단어 경계로만 — 한글 본문 속 우연한 부분일치를 배제한다.
            and re.search(r"(?<![A-Za-z0-9_])" + re.escape(t.lower()) + r"(?![A-Za-z0-9_])", low)
        ]
        print(f"{case_dir.name}: " + (f"누출 {len(hits)}건 → {sorted(hits)}" if hits else "OK"))
        total += len(hits)

    print(f"\n총 누출 후보: {total}건")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="업무정의서 구현 어휘 누출 검사")
    ap.add_argument(
        "--goldset",
        default=r"C:\Users\qoqkd\Desktop\final-etc-files\골드셋",
        help="골드셋 루트 (정답셋/·업무정의서_정규화/ 를 포함하는 디렉터리)",
    )
    args = ap.parse_args()
    return 0 if check(Path(args.goldset)) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
