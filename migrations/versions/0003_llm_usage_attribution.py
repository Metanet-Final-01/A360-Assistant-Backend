"""llm_usage attribution columns (RPA-33)

LLM 사용량을 사용자별·시스템·컴포넌트별로 집계하기 위해 llm_usage에
actor_type / user_id / component 추가 (server_default 포함).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-07 10:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_usage",
        sa.Column(
            "actor_type",
            sa.Enum("user", "system", name="usage_actor_type", native_enum=False),
            server_default="system",
            nullable=False,
        ),
    )
    op.add_column("llm_usage", sa.Column("user_id", sa.UUID(), nullable=True))
    op.add_column(
        "llm_usage",
        sa.Column("component", sa.String(length=30), server_default="other", nullable=False),
    )
    op.create_index(op.f("ix_llm_usage_actor_type"), "llm_usage", ["actor_type"], unique=False)
    op.create_index(op.f("ix_llm_usage_component"), "llm_usage", ["component"], unique=False)
    op.create_index(op.f("ix_llm_usage_user_id"), "llm_usage", ["user_id"], unique=False)
    op.create_foreign_key(
        "llm_usage_user_id_fkey", "llm_usage", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("llm_usage_user_id_fkey", "llm_usage", type_="foreignkey")
    op.drop_index(op.f("ix_llm_usage_user_id"), table_name="llm_usage")
    op.drop_index(op.f("ix_llm_usage_component"), table_name="llm_usage")
    op.drop_index(op.f("ix_llm_usage_actor_type"), table_name="llm_usage")
    op.drop_column("llm_usage", "component")
    op.drop_column("llm_usage", "user_id")
    op.drop_column("llm_usage", "actor_type")
