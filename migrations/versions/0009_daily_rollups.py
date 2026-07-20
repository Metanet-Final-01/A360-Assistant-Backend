"""metrics_daily·usage_daily — 일별 롤업 집계 (RPA-104)

관측 DB(Neon)가 주 저장소지만, OBSERVABILITY_DATABASE_URL 미설정 시 앱 DB로
폴백하므로 앱 DB에도 동일 테이블을 만든다 (request_metrics와 같은 패턴).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-10 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metrics_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("err_4xx", sa.Integer(), nullable=False),
        sa.Column("err_5xx", sa.Integer(), nullable=False),
        sa.Column("p50_ms", sa.Integer(), nullable=True),
        sa.Column("p95_ms", sa.Integer(), nullable=True),
        sa.Column("avg_ms", sa.Integer(), nullable=True),
        sa.Column("max_ms", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day", "method", "path"),
    )
    op.create_table(
        "usage_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("component", sa.String(length=30), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("day", "component", "purpose", "model"),
    )


def downgrade() -> None:
    op.drop_table("usage_daily")
    op.drop_table("metrics_daily")
