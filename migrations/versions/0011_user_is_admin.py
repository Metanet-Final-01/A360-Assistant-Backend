"""users.is_admin — 관리자 인가를 서버 속성으로 (RPA-118)

문자열 화이트리스트(ADMIN_EMAILS) + 개방 가입에 의존하던 인가를 DB 속성으로 옮긴다.
ADMIN_EMAILS는 이 값을 세팅하는 부트스트랩 시드로만 남는다(앱 기동 시 백필).

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-13 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "is_admin")
