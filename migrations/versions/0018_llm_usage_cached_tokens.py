# -*- coding: utf-8 -*-
"""llm_usage.cached_tokens — 프롬프트 캐시 적중분 기록 (RPA-199)

비용 계산이 실청구와 4.7배 어긋난 주원인: 캐시된 입력(정가의 10%)을 전액으로 계산했다.
OpenAI 응답의 usage.prompt_tokens_details.cached_tokens를 받아 기록하고, 비용식이
(input−cached)×정가 + cached×캐시단가로 갈라 계산한다.

nullable인 이유: 과거 행은 "측정 안 함"(NULL)이지 "캐시 0"이 아니다 — 런타임 값이라
소급 복원이 불가능하고(request_id와 동일, RPA-158), 이 구분이 있어야 실청구서 대사에서
모르는 구간을 정직하게 표시한다.

관측 DB 쪽은 ensure_observability_schema()의 idempotent ALTER가 보장한다(request_id 선례).
**이 마이그레이션은 폴백용** — OBSERVABILITY_DATABASE_URL 미설정이면 관측 쓰기가 앱 DB로
떨어지므로, 로컬 단독 개발에서도 이 컬럼이 있어야 한다.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-18 18:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("cached_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "cached_tokens")
