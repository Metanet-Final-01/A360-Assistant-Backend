"""analysis_sessions.solution — 자동화 대상 솔루션 (에이전트 그래프·RAG 카탈로그 라우팅 키)

에이전트 단일 진입점에서 매 턴 그대로 전달하는 결정론적 키. 업로드 시점에 확정 저장한다.
기존 세션은 A360 기준으로 만들어졌으므로 server_default="a360"로 백필한다.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-08 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analysis_sessions",
        sa.Column("solution", sa.String(length=50), nullable=False, server_default="a360"),
    )


def downgrade() -> None:
    op.drop_column("analysis_sessions", "solution")
