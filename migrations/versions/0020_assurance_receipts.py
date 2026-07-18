# -*- coding: utf-8 -*-
"""assurance_receipts — 보증 판정 증거 영속화 (RPA-182)

일반 관측 로그는 best-effort라 장애 시 유실될 수 있다. 향후 저장·latest·export의 차단 근거가
될 영수증은 제품 DB에 두고, UPDATE/DELETE를 trigger로 거부해 애플리케이션 경로에서 append-only를
강제한다. 원문 추천 payload와 사용자 텍스트는 저장하지 않고 digest·판정·최소 증거만 보존한다.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-19 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assurance_receipts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("receipt_digest", sa.String(length=71), nullable=False, unique=True),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("harness", sa.String(length=20), nullable=False),
        sa.Column("record_kind", sa.String(length=40), nullable=False),
        sa.Column("writer_authority", sa.String(length=60), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("session_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("recommendation_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("recommendation_version", sa.Integer(), nullable=True),
        sa.Column("candidate_id", sa.String(length=71), nullable=True),
        sa.Column("payload_digest", sa.String(length=71), nullable=True),
        sa.Column("source_observation_id", sa.String(length=71), nullable=True),
        sa.Column("evidence_valid", sa.Boolean(), nullable=False),
        sa.Column("completeness_status", sa.String(length=20), nullable=False),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("assurance_verdict", sa.String(length=20), nullable=False),
        sa.Column("assurance_status", sa.String(length=40), nullable=False),
        sa.Column("rollout_mode", sa.String(length=20), nullable=False),
        sa.Column("enforcement_effect", sa.String(length=20), nullable=False),
        sa.Column("business_persisted", sa.Boolean(), nullable=True),
        sa.Column("validator_version", sa.String(length=100), nullable=True),
        sa.Column("policy_digest", sa.String(length=71), nullable=True),
        sa.Column("catalog_digest", sa.String(length=71), nullable=True),
        sa.Column("requested_agent_version", sa.String(length=50), nullable=True),
        sa.Column("resolved_agent_version", sa.String(length=50), nullable=True),
        sa.Column("receipt_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("harness IN ('change', 'output')", name="ck_assurance_receipts_harness"),
        sa.CheckConstraint(
            "completeness_status IN ('complete', 'incomplete')",
            name="ck_assurance_receipts_completeness",
        ),
        sa.CheckConstraint(
            "assurance_verdict IN ('observed', 'deny', 'refused')",
            name="ck_assurance_receipts_verdict",
        ),
        sa.CheckConstraint(
            "rollout_mode IN ('observe', 'warn', 'enforce')",
            name="ck_assurance_receipts_rollout",
        ),
        sa.CheckConstraint(
            "enforcement_effect IN ('none', 'warned', 'blocked')",
            name="ck_assurance_receipts_effect",
        ),
    )
    for name, columns in (
        ("ix_assurance_receipts_harness", ["harness"]),
        ("ix_assurance_receipts_request_id", ["request_id"]),
        ("ix_assurance_receipts_session_id", ["session_id"]),
        ("ix_assurance_receipts_recommendation_id", ["recommendation_id"]),
        ("ix_assurance_receipts_candidate_id", ["candidate_id"]),
        ("ix_assurance_receipts_decision", ["decision"]),
        ("ix_assurance_receipts_assurance_verdict", ["assurance_verdict"]),
        ("ix_assurance_receipts_created_at", ["created_at"]),
    ):
        op.create_index(name, "assurance_receipts", columns)

    op.execute("""
        CREATE FUNCTION reject_assurance_receipt_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'assurance_receipts is append-only';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_assurance_receipts_append_only
        BEFORE UPDATE OR DELETE ON assurance_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_assurance_receipt_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_assurance_receipts_append_only ON assurance_receipts")
    op.execute("DROP FUNCTION IF EXISTS reject_assurance_receipt_mutation()")
    op.drop_table("assurance_receipts")
