"""llm_usage.request_id — 턴 단위 비용 귀속 (RPA-158)

llm_usage에 request_id가 없어 audit_logs·turn_events·rag_events(모두 request_id 보유)와
조인 불가 → "이 턴이 얼마 들었나"에 답 못 하고 session 총액까지만이었다. 같은 request_id로
비용을 턴에 묶기 위해 컬럼을 추가한다. 이 마이그레이션은 앱 DB용 — 관측 전용 DB(Neon)는
ensure_observability_schema()가 기동 시 idempotent ALTER로 동일 컬럼을 보장한다.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-14 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("request_id", sa.String(length=32), nullable=True))
    op.create_index(op.f("ix_llm_usage_request_id"), "llm_usage", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_usage_request_id"), table_name="llm_usage")
    op.drop_column("llm_usage", "request_id")
