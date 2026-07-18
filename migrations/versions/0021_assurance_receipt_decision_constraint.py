# -*- coding: utf-8 -*-
"""assurance_receipts decision 허용값 강제 (RPA-182)

0020은 PR 머지 전 공유 DB에 적용된 이력이 있어 수정하지 않는다. 새 revision으로 서비스의
정규화 계약과 DB 제약을 일치시켜 기존 환경과 신규 환경이 같은 migration chain을 따른다.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-19 01:30:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_assurance_receipts_decision",
        "assurance_receipts",
        "decision IN ('allow_candidate', 'deny', 'unassured')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_assurance_receipts_decision", "assurance_receipts", type_="check"
    )
