"""검색 파라미터 런타임 튜닝 스키마 (RPA-149) — admin API PUT 본문/응답.

값의 의미·검증 규칙은 app/rag/retrieval/params.py의 RetrievalParams가 단일 진실이다.
여기서는 요청 형태(타입·필드 존재)만 잡고, 값 범위(1 이상·유한·비음수)는 라우터가
RetrievalParams(**body)로 생성하며 __post_init__에 위임한다 — 검증 규칙 이중 정의를 피한다.
"""

from pydantic import BaseModel, Field


class RetrievalParamsUpdate(BaseModel):
    """PUT /api/admin/retrieval-params 본문. 5개 노브 모두 필수(부분 갱신 아님 — 전체 스냅샷).

    부분 갱신을 안 받는 이유: append-only 이력에 "그 시점의 완전한 설정"을 남겨야 감사·롤백이
    명확하다(누락 필드가 이전 행에서 암묵 상속되면 어떤 조합으로 돌았는지 추적이 흐려진다).
    """

    candidate_pool_size: int = Field(description="벡터·BM25 각 branch 후보 수(RRF 입력 폭), 1 이상")
    rerank_candidates: int = Field(description="RRF 융합 후 리랭커에 넘길 상한, 1 이상")
    rrf_k: int = Field(description="RRF 상수 k(클수록 상위 순위 가중 완만), 1 이상")
    vector_weight: float = Field(description="RRF 벡터(의미) branch 가중치, 0 이상 유한")
    bm25_weight: float = Field(description="RRF BM25(키워드) branch 가중치, 0 이상 유한")
