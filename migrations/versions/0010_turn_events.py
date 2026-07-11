"""turn_events — 에이전트 턴 노드 타임라인 (RPA-105)

관측 DB(Neon)가 주 저장소, OBSERVABILITY_DATABASE_URL 미설정 시 앱 DB 폴백이라
앱 DB에도 동일 테이블 (request_metrics·롤업과 같은 패턴).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-10 19:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=True),
        sa.Column("message", sa.String(length=512), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_turn_events_session_id"), "turn_events", ["session_id"], unique=False)
    op.create_index(op.f("ix_turn_events_request_id"), "turn_events", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_turn_events_request_id"), table_name="turn_events")
    op.drop_index(op.f("ix_turn_events_session_id"), table_name="turn_events")
    op.drop_table("turn_events")
