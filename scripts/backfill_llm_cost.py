"""llm_usage.cost_usd 결측 백필 (RPA-158, G2).

가격 env(LLM_INPUT/OUTPUT_COST_PER_1M)가 설정되기 전 기록된 챗 호출은 토큰만 있고
cost_usd=NULL로 남아 과거 비용 리포트가 과소 집계된다. 토큰과 단가표는 그대로이므로
cost_usd()로 재계산해 채운다 — 단가가 안 바뀌었으므로 이는 '추정'이 아니라 '복원'이다.
단가표/env에 없는 모델은 여전히 None이면 건너뛴다(임의 값을 만들지 않음).

⚠️ 공유 관측 DB(Neon)에 쓴다. 기본은 dry-run, --apply를 줘야 실제 갱신한다.
실행: PYTHONIOENCODING=utf-8 python -m scripts.backfill_llm_cost [--apply]
"""

import argparse

from app.core.llm import cost_usd
from app.core.observability_db import observability_sessionmaker
from app.models import LlmUsage


def main(apply: bool) -> None:
    with observability_sessionmaker()() as db:
        rows = db.query(LlmUsage).filter(LlmUsage.cost_usd.is_(None)).all()
        candidates = []
        for r in rows:
            if not (r.input_tokens or r.output_tokens):
                continue  # 토큰이 없으면 복원할 비용도 없음
            new_cost = cost_usd(r.input_tokens, r.output_tokens, r.model)
            if new_cost is None:
                continue  # 단가표/env에 없는 모델 — 임의 값을 만들지 않고 건너뜀
            candidates.append((r, new_cost))

        total = sum(c for _, c in candidates)
        print(
            f"cost_usd NULL 행: {len(rows)} / 복원 가능(토큰+단가 있음): {len(candidates)} "
            f"/ 복원 비용 합: ${total:.6f}"
        )
        for r, c in candidates[:10]:
            print(f"  id={r.id} model={r.model} in={r.input_tokens} out={r.output_tokens} -> ${c:.6f}")

        if not apply:
            print("dry-run — 실제 갱신하려면 --apply 를 붙여 다시 실행하세요.")
            return

        for r, c in candidates:
            r.cost_usd = c
        db.commit()
        print(f"적용 완료: {len(candidates)}행 cost_usd 갱신.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="llm_usage cost_usd 결측 백필 (RPA-158)")
    ap.add_argument("--apply", action="store_true", help="실제 DB 갱신 (미지정 시 dry-run)")
    main(ap.parse_args().apply)
