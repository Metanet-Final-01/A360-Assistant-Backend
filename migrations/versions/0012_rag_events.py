"""rag_events — RAG 파이프라인 단계 로그 중앙화 (RPA-128)

로컬 JSONL(app/rag/logs)에만 있던 embed/search/rerank 단계 로그를 관측 DB로.
OBSERVABILITY_DATABASE_URL 미설정 시 앱 DB로 폴백하므로 앱 DB에도 만든다.
FK 없음 — 순수 관측 이벤트.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-13 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rag_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("function", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rag_events_request_id"), "rag_events", ["request_id"], unique=False)
    op.create_index(op.f("ix_rag_events_created_at"), "rag_events", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_rag_events_created_at"), table_name="rag_events")
    op.drop_index(op.f("ix_rag_events_request_id"), table_name="rag_events")
    op.drop_table("rag_events")
