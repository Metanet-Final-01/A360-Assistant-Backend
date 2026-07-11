"""request_metrics — 모든 요청 성능 메트릭 (RPA-103)

관측 DB(Neon)가 주 저장소지만, OBSERVABILITY_DATABASE_URL 미설정 시 앱 DB로
폴백하므로 앱 DB에도 동일 테이블을 만든다 (audit_logs와 같은 패턴).
FK 없음 — 순수 메트릭(정규화된 path)이라 참조 무결성 불필요.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-10 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_request_metrics_path"), "request_metrics", ["path"], unique=False)
    op.create_index(op.f("ix_request_metrics_created_at"), "request_metrics", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_request_metrics_created_at"), table_name="request_metrics")
    op.drop_index(op.f("ix_request_metrics_path"), table_name="request_metrics")
    op.drop_table("request_metrics")
