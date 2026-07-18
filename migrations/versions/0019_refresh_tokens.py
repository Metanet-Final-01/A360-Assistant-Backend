# -*- coding: utf-8 -*-
"""refresh_tokens — 갱신 토큰 (RPA-200)

액세스 토큰이 60분 만료 단일 발급이라 사용자가 한 시간마다 재로그인해야 했다.
스테이트리스 리프레시 JWT 대신 테이블을 두는 이유는 **폐기**다 — 로그아웃·탈취 대응이
"클라이언트가 지웠다"는 주장에 그치면 안 된다.

원문이 아니라 **해시(SHA-256 hex 64자)만** 저장한다: DB가 유출돼도 세션을 만들 수 없어야 한다.
행을 지우지 않고 revoked_at으로 표시하는 이유는 **재사용 탐지** — 이미 폐기된 토큰이 다시
제시되면 탈취 신호로 보고 그 사용자의 토큰 전체를 폐기한다(OAuth 2.0 Security BCP).

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-18 21:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 한 로그인에서 회전으로 이어지는 토큰들의 계열 — 로그아웃·탈취 대응의 단위다
        sa.Column("family_id", sa.UUID(as_uuid=True), nullable=False),
        # unique — 같은 토큰이 두 행으로 갈리면 폐기가 한쪽만 걸린다
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
