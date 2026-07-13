"""검색 평가 골드셋 로더 (RPA-131).

라벨 출처는 GitHub에서 수집한 A360 봇들(data/ingest/bots.jsonl)이 실제 쓰는 액션이다 —
합성 쿼리가 아니라 '현업 자동화가 정말 사용하는 액션'이 정답이라 실무 적합도가 높다.
정답 키는 doc id가 아니라 (package_name, action_name)이다: 재적재(재크롤·재임베딩) 후
id는 바뀌지만 패키지/액션 표기는 안정적이므로 골드셋이 깨지지 않는다.
"""

import json
from dataclasses import dataclass
from pathlib import Path

# 코퍼스에 있는 (package_name, action_name)을 정답 키로 쓴다 — 봇의 packageName/commandName과
# 정확히 같은 표기(예: Excel_MS/OpenSpreadsheet).
ActionKey = tuple[str, str]

# 큐레이션된 평가 자산이라 패키지와 함께 버전 관리한다(대용량 수집 산출물 data/는 gitignore).
_DEFAULT_PATH = Path(__file__).resolve().parent / "retrieval_goldset.jsonl"


@dataclass(frozen=True)
class GoldQuery:
    """평가 쿼리 하나 — 자연어 의도(query)와 그에 맞는 정답 액션 집합(relevant)."""

    query: str
    relevant: frozenset[ActionKey]
    note: str = ""


def doc_key(doc: dict) -> ActionKey | None:
    """검색 결과 문서에서 (package_name, action_name) 정답 키를 뽑는다.

    action_name이 없는 문서(패키지 개요 등)는 액션 단위 정답과 맞출 수 없으므로 None."""
    package = doc.get("package_name")
    action = doc.get("action_name")
    if package and action:
        return (package, action)
    return None


def load_goldset(path: str | Path | None = None) -> list[GoldQuery]:
    """골드셋 JSONL을 읽어 GoldQuery 리스트로. 각 줄: {query, relevant:[{package,action}], note?}.

    relevant가 비었거나 형식이 어긋난 줄은 측정에서 의미가 없으므로 명시적으로 막는다
    (조용히 건너뛰면 '정답 0개'가 MRR을 왜곡한다)."""
    path = Path(path) if path is not None else _DEFAULT_PATH
    queries: list[GoldQuery] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            relevant = frozenset(
                (r["package"], r["action"]) for r in row.get("relevant", [])
            )
            if not row.get("query") or not relevant:
                raise ValueError(f"골드셋 {lineno}번째 줄: query와 relevant는 비어 있을 수 없습니다")
            queries.append(GoldQuery(query=row["query"], relevant=relevant, note=row.get("note", "")))
    return queries
