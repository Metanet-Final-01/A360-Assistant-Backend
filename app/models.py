"""도메인 ORM 모델.

설계 결정: 추천안(Recommendation)은 버전 관리되는 상태 객체다.
챗봇 수정·드래그 수정·피드백 반영이 전부 "같은 세션의 새 버전 생성"으로 통일되어
FR-15(맥락 유지)와 undo를 자연스럽게 지원한다. AI 산출물 본문(JSONB)의 구조는
app/schemas 의 Pydantic 모델이 정의한다.

RAG 지식베이스(rag_documents)는 app/rag 가 원시 SQL로 관리하므로 여기 없다.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    """인증 사용자 (이메일/비밀번호). password_hash에는 bcrypt 해시만 저장한다."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalysisSession(Base):
    """분석 세션 — 업로드부터 내보내기까지의 한 사이클 (FR-20 이력 관리 단위)."""

    __tablename__ = "analysis_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 세션 소유자. 비로그인 업로드로 만든 세션은 NULL(익명) — 하위호환. 소유자가 있으면
    # 접근 라우트가 요청자와 일치하는지 검사한다 (남의 세션 UUID로 접근 차단).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="session")
    recommendations: Mapped[list["RecommendationVersion"]] = relationship(back_populates="session")


class Document(Base):
    """업로드된 업무정의서 (FR-01~04)."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(
        Enum("uploaded", "parsing", "parsed", "failed", name="document_status", native_enum=False),
        default="uploaded",
    )
    masked: Mapped[bool] = mapped_column(Boolean, default=False)  # 민감정보 마스킹 적용 여부
    parsed_content: Mapped[dict | None] = mapped_column(JSONB)  # FR-04 구조화 추출 결과
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[AnalysisSession] = relationship(back_populates="documents")


class Analysis(Base):
    """LLM 업무 흐름 분석 실행 및 결과 (FR-05, 08). result는 schemas.AnalysisResult."""

    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(
        Enum("pending", "running", "completed", "failed", name="analysis_status", native_enum=False),
        default="pending",
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))


class RecommendationVersion(Base):
    """추천안 버전 (FR-09~15). payload는 schemas.Recommendation.

    수정은 UPDATE가 아니라 새 버전 INSERT다. source가 버전의 출처를 말한다:
    agent(최초 생성)/chat(챗봇 수정)/drag(UI 드래그 수정)/feedback(피드백 반영).
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint("session_id", "version", name="uq_recommendations_session_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="CASCADE"), index=True
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    parent_version: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(
        Enum("agent", "chat", "drag", "feedback", name="recommendation_source", native_enum=False)
    )
    payload: Mapped[dict] = mapped_column(JSONB)
    change_summary: Mapped[str | None] = mapped_column(Text)  # 예: "Task4를 Email 액션으로 교체"
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[AnalysisSession] = relationship(back_populates="recommendations")


class ChatMessage(Base):
    """챗봇 대화 이력 (FR-15). 추천 버전을 만든 메시지는 그 버전 번호를 기록한다."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(
        Enum("user", "assistant", "system", name="chat_role", native_enum=False)
    )
    content: Mapped[str] = mapped_column(Text)
    recommendation_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    """추천 결과 피드백 (추가 기능). applied는 이후 추천 생성에 반영됐는지 표시."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="CASCADE"), index=True
    )
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE")
    )
    step_id: Mapped[str | None] = mapped_column(String(50))  # 특정 단계 피드백이면 지정
    rating: Mapped[str] = mapped_column(
        Enum("good", "bad", "needs_fix", name="feedback_rating", native_enum=False)
    )
    comment: Mapped[str | None] = mapped_column(Text)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmUsage(Base):
    """LLM 호출 사용량 (비기능: 관측성 — 토큰/비용/응답시간 모니터링).

    3축으로 집계 가능: user_id(누가) / component(어느 서브시스템) / model(어느 LLM).
    actor_type='system'은 사용자와 무관한 백그라운드 사용(RAG 임베딩 적재 등).
    """

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="SET NULL"), index=True
    )
    # 누가 유발했나 — user(사람) / system(임베딩 적재 등 백그라운드). user_id는 익명·시스템이면 NULL
    # server_default: NOT NULL 컬럼을 기존 로우에 ALTER ADD 하거나 ORM 외 직접 INSERT할 때도
    # 안전하도록 DB 레벨 기본값을 준다 (default=는 ORM 삽입 시에만 적용됨)
    actor_type: Mapped[str] = mapped_column(
        Enum("user", "system", name="usage_actor_type", native_enum=False),
        default="system",
        server_default="system",
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # 어느 서브시스템 — vision|agent|rag_embed|rag_rerank|other
    component: Mapped[str] = mapped_column(
        String(30), default="other", server_default="other", index=True
    )
    purpose: Mapped[str] = mapped_column(String(50))  # analyze|recommend|chat|summarize|vision_parse|embed|other
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalRun(Base):
    """골드셋 평가 실행 기록 (요구사항 8.2 자가검증). metrics 예:
    {"retrieval_hit_at_5": 0.83, "action_accuracy": 0.71}
    """

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(255))
    goldset_version: Mapped[str | None] = mapped_column(String(50))
    git_sha: Mapped[str | None] = mapped_column(String(40))
    metrics: Mapped[dict] = mapped_column(JSONB)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
