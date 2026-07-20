"""retrieval_params — 검색 파라미터 런타임 오버라이드 (RPA-149)

config가 import 시점에 상수로 고정돼 값 변경이 재시작을 요구하던 것을, admin API가 쓰고
검색 경로가 읽는 단일 행(최신 우선)으로 옮겨 무중단 튜닝을 가능케 한다. append-only —
updated_by/created_at으로 변경 이력을 겸한다. 행이 없으면 검색은 .env로 폴백하므로 이
마이그레이션만으로는 동작이 바뀌지 않는다(안전). 앱 DB에 만든다(앱 설정, 관측 DB 아님).

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-14 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "retrieval_params",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("candidate_pool_size", sa.Integer(), nullable=False),
        sa.Column("rerank_candidates", sa.Integer(), nullable=False),
        sa.Column("rrf_k", sa.Integer(), nullable=False),
        sa.Column("vector_weight", sa.Float(), nullable=False),
        sa.Column("bm25_weight", sa.Float(), nullable=False),
        sa.Column("updated_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_retrieval_params_created_at"), "retrieval_params", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_retrieval_params_created_at"), table_name="retrieval_params")
    op.drop_table("retrieval_params")
