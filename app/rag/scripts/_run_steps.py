"""옵션 스크립트(run_option*.py)들이 공유하는 "pipeline 서브커맨드를 순서대로 실행"
헬퍼. 각 단계는 이미 테스트된 `python -m app.rag.pipeline <command>`를 그대로
호출한다 — 로직을 다시 구현하지 않고 기존 CLI를 순서대로 이어붙이기만 한다.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def run_steps(steps: list[list[str]]) -> None:
    """steps: 각 원소가 `python -m app.rag.pipeline` 뒤에 붙일 인자 리스트.
    한 단계라도 실패하면(0이 아닌 종료 코드) 그 자리에서 멈춘다 — 이전 단계가 실패했는데
    다음 단계를 계속 진행하면 절반만 된 데이터로 조용히 넘어가게 된다.
    """
    for i, args in enumerate(steps, 1):
        print(f"\n=== [{i}/{len(steps)}] {' '.join(args)} ===")
        result = subprocess.run(
            [sys.executable, "-m", "app.rag.pipeline", *args],
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            sys.exit(f"[{i}/{len(steps)}] 실패 (종료 코드 {result.returncode}) — 여기서 중단합니다: {' '.join(args)}")
