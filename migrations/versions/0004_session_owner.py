"""analysis_sessions.user_id — 세션 소유자 (RPA-40)

세션 소유권 검사용. nullable(익명 세션 허용), users 삭제 시 SET NULL.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-07 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("analysis_sessions", sa.Column("user_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_analysis_sessions_user_id"), "analysis_sessions", ["user_id"], unique=False
    )
    op.create_foreign_key(
        "analysis_sessions_user_id_fkey",
        "analysis_sessions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("analysis_sessions_user_id_fkey", "analysis_sessions", type_="foreignkey")
    op.drop_index(op.f("ix_analysis_sessions_user_id"), table_name="analysis_sessions")
    op.drop_column("analysis_sessions", "user_id")
