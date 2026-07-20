"""budget_limits — 예산 상한 런타임 오버라이드 (RPA-173)

RPA-171의 상한이 .env에만 있어 바꾸려면 재배포가 필요했다. RPA-149(retrieval_params)와 같은
패턴으로 DB 오버라이드를 둬, 모니터링(백오피스)이 admin API로 무중단 조정하게 한다.

append-only — 행마다 updated_by/created_at을 남겨 "누가 언제 상한을 얼마로 바꿨나"가 이력으로
남는다(예산은 서비스를 막는 값이라 이 감사가 특히 중요하다). 활성값은 최신 행(id DESC).
행이 없으면 .env(BUDGET_*_USD)로 폴백 — 오버라이드 없는 배포는 무변경.

상한 값은 nullable: NULL = 그 상한 비활성(.env의 '미설정=비활성'과 같은 의미).

이 테이블은 **앱 DB**다(관측 DB 아님) — 설정이지 관측 데이터가 아니고, retrieval_params와
같은 자리에 둬야 "런타임 튜닝 설정"이 한곳에 모인다.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-15 06:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "budget_limits",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_daily_usd", sa.Float(), nullable=True),
        sa.Column("subject_monthly_usd", sa.Float(), nullable=True),
        sa.Column("global_daily_usd", sa.Float(), nullable=True),
        sa.Column("global_monthly_usd", sa.Float(), nullable=True),
        sa.Column("updated_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 값 자체를 DB가 지킨다 — API(BudgetLimitsUpdate)가 이미 막지만, 직접 SQL 등 우회 경로로
        # 이상값이 들어오면 상한이 조용히 무력화된다: nan은 서비스에서 비활성으로, inf는 "영원히
        # 초과 안 됨"이 된다 (#243 리뷰).
        # ⚠️ Postgres는 **NaN을 모든 수보다 크게** 취급해 `> 0`으로 안 걸러진다(Python과 반대).
        #    `< 'Infinity'`가 NaN(비교 False)과 inf를 함께 막는다.
        *[
            sa.CheckConstraint(
                f"{c} IS NULL OR ({c} > 0 AND {c} < 'Infinity'::float8)",
                name=f"ck_budget_limits_{c}_positive_finite",
            )
            for c in ("subject_daily_usd", "subject_monthly_usd",
                      "global_daily_usd", "global_monthly_usd")
        ],
    )
    op.create_index("ix_budget_limits_created_at", "budget_limits", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_budget_limits_created_at", table_name="budget_limits")
    op.drop_table("budget_limits")
