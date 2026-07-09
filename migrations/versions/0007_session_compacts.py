"""session_compacts — 대화 압축본 (RPA-66)

대화가 길어지면 압축 노드가 이력을 고정 섹션 JSON으로 요약해 저장한다.
매 턴 최신 압축본을 컨텍스트로 주입하고, 압축 이후 대화만 이력으로 넘긴다.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-09 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_compacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False, server_default="1.0"),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["analysis_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_session_compacts_session_id"), "session_compacts", ["session_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_session_compacts_session_id"), table_name="session_compacts")
    op.drop_table("session_compacts")
