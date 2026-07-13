"""하이브리드 검색 튜닝 노브 한 묶음 — 하이퍼파라미터 탐색(sweep)이 올라탈 기반 (RPA-130).

config.py는 이 값들을 import 시점에 모듈 상수로 굳혀서, 한 프로세스 안에서 값을 바꿔
끼울 수 없다(스윕 불가). RetrievalParams는 리트리버·리랭커·RRF 파라미터를 한 객체로 모아
search()에 관통시켜, 골드셋으로 조합별 MRR을 측정할 수 있게 한다.

호출부가 params를 안 넘기면 from_config()가 현재 .env 값으로 복원하므로 기존 동작과
100% 동일하다(하위호환). config를 참조 시점에 읽으므로 테스트가 config 값을 monkeypatch하면
그대로 반영된다.
"""

from dataclasses import dataclass

from .. import config


@dataclass(frozen=True)
class RetrievalParams:
    """하이브리드 검색의 조절 가능한 파라미터.

    - candidate_pool_size: 벡터·BM25 각 branch에서 가져올 후보 수(RRF 입력 폭).
    - rerank_candidates: RRF 융합 후 리랭커에 넘길 상한(재정렬 비용/품질 트레이드오프).
    - rrf_k: RRF 상수 k — 클수록 상위 순위 가중이 완만해진다.
    - vector_weight / bm25_weight: RRF branch별 가중치. 지금까지 동일(1.0)로 하드코딩돼
      벡터·키워드 신호 비중을 조절할 수 없었다 — 탐색 대상으로 연다.
    최종 반환 개수(top_k)와 mode는 search()의 호출부 인자(limit/mode)로 남는다 —
    파라미터는 '검색 품질 튜닝', limit/mode는 '요청 형태'로 관심사를 분리한다.
    """

    candidate_pool_size: int
    rerank_candidates: int
    rrf_k: int
    vector_weight: float = 1.0
    bm25_weight: float = 1.0

    @classmethod
    def from_config(cls) -> "RetrievalParams":
        """현재 .env(config) 값으로 기본 파라미터를 복원한다 — params 미지정 시의 기존 동작."""
        return cls(
            candidate_pool_size=config.HYBRID_CANDIDATE_POOL_SIZE,
            rerank_candidates=config.HYBRID_RERANK_CANDIDATES,
            rrf_k=config.RRF_K,
        )
