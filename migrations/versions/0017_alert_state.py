"""alert_state — 알림 전이·쿨다운 상태 (RPA-189)

"이 알림을 이미 보냈나"를 기억한다. 없으면 같은 사유로 슬랙을 계속 쏜다 —
/health가 30초마다 degraded면 하루 2,880개다.

이 테이블은 **관측 DB**다(앱 DB 아님) — 설정이 아니라 관측 파생 상태이고, 알림의 근거
데이터(metrics_daily·usage_daily·llm_usage)가 전부 거기 있어 같은 세션에서 읽고 쓴다.
관측 DB 쪽 생성은 ensure_observability_schema()의 create_all이 맡는다(별도 alembic 체인 없음).
**이 마이그레이션이 필요한 이유는 폴백** — OBSERVABILITY_DATABASE_URL 미설정이면 관측 쓰기가
앱 DB로 떨어지므로(app/core/observability_db.py), 로컬 단독 개발에서도 이 테이블이 있어야 한다.

⚠️ 인메모리로 두면 안 되는 이유: (a) 재시작마다 재알림 (b) uvicorn --workers N이면 N개
프로세스가 각자 보낸다. "스로틀했다"고 주장하려면 그 상태를 **모든 발신자가 함께 보는 곳**에
둬야 한다 — 가드가 읽는 것과 동작이 읽는 것이 같아야 한다(CONVENTIONS §9).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-16 12:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_state",
        # 알림 종류 식별자. 같은 key의 status가 바뀌면(ok↔firing) 전이로 보고 알린다.
        #   예: "budget:global:daily" · "budget:subject:<uuid>:daily" · "health:degraded"
        # 주체 uuid(36자)가 섞여 들어가므로 넉넉히 잡는다.
        sa.Column("key", sa.String(length=120), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False),  # ok | firing
        # 마지막 발송 시각 — 같은 status가 이어져도 쿨다운이 지나면 재알림("아직도 터져 있다").
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),  # 마지막으로 보낸 내용(디버깅용)
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("alert_state")
