"""llm_usage 예산 조회 인덱스 — (user_id, created_at)·(session_id, created_at) (RPA-171)

예산 가드레일이 매 턴 진입 시 "이 주체가 이 기간에 얼마 썼나"를 llm_usage에서 집계한다.
요청 경로라 인덱스가 없으면 llm_usage가 커질수록 턴 지연이 함께 커진다.

기존 인덱스는 request_id·session_id 단독뿐이고 **user_id는 인덱스가 아예 없었다**. 예산 쿼리는
항상 기간 필터(created_at >= 일/월 시작)가 붙으므로 복합 인덱스로 만든다.

이 마이그레이션은 앱 DB용 — 관측 전용 DB(Neon)는 ensure_observability_schema()가 기동 시
idempotent CREATE INDEX IF NOT EXISTS로 동일 인덱스를 보장한다 (RPA-158과 같은 이원 구조).

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-15 03:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 주체별 상한 — 주체 + 기간
    op.create_index(
        "ix_llm_usage_user_created", "llm_usage", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_llm_usage_session_created", "llm_usage", ["session_id", "created_at"], unique=False
    )
    # 전역 상한 — 주체 필터 없이 기간만으로 합산한다. 위 두 인덱스는 주체가 선행이라 range
    # seek이 안 걸리므로, 전용 인덱스가 없으면 전역 상한을 켜는 순간 매 턴 full scan (#239 리뷰).
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_index("ix_llm_usage_session_created", table_name="llm_usage")
    op.drop_index("ix_llm_usage_user_created", table_name="llm_usage")
