"""검색 순위 평가 지표 — 순위 정답 여부(hits) 위에서만 동작하는 순수 함수 (RPA-131).

hits는 검색 결과를 상위 순서대로 훑으며 각 문서가 정답(relevant)인지를 담은 bool 리스트다
(hits[0]=1위). 지표는 이 리스트에만 의존하므로 검색 구현·DB와 무관하게 단위 테스트된다.
"""


def first_relevant_rank(hits: list[bool]) -> int | None:
    """정답이 처음 등장하는 1-기반 순위. 상위에 정답이 하나도 없으면 None."""
    for rank, is_relevant in enumerate(hits, start=1):
        if is_relevant:
            return rank
    return None


def reciprocal_rank(hits: list[bool]) -> float:
    """역순위(RR) = 1 / 첫 정답 순위. 정답이 없으면 0.0.

    MRR은 이 값의 쿼리 평균 — "정답을 얼마나 위쪽에 올리나"를 본다(순위에 민감)."""
    rank = first_relevant_rank(hits)
    return 1.0 / rank if rank is not None else 0.0


def recall_at_k(hits: list[bool], total_relevant: int, k: int) -> float:
    """상위 k개 안에서 찾은 정답 수 / 전체 정답 수. total_relevant<=0이면 0.0.

    상위 k에 걸린 정답만 세므로 값은 [0, 1]. 정답이 여러 개인 쿼리의 '놓친 정답'을 본다."""
    if total_relevant <= 0 or k <= 0:
        return 0.0
    found = sum(1 for is_relevant in hits[:k] if is_relevant)
    return found / total_relevant


def hit_at_k(hits: list[bool], k: int) -> float:
    """상위 k 안에 정답이 하나라도 있으면 1.0, 없으면 0.0 (성공/실패 이진 지표)."""
    if k <= 0:
        return 0.0
    return 1.0 if any(hits[:k]) else 0.0


def mean(values: list[float]) -> float:
    """쿼리별 지표를 집계한다. 빈 리스트는 0.0(측정 대상 없음)."""
    return sum(values) / len(values) if values else 0.0
