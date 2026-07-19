"""도메인 ORM 모델.

설계 결정: 추천안(Recommendation)은 버전 관리되는 상태 객체다.
챗봇 수정·드래그 수정·피드백 반영이 전부 "같은 세션의 새 버전 생성"으로 통일되어
FR-15(맥락 유지)와 undo를 자연스럽게 지원한다. AI 산출물 본문(JSONB)의 구조는
app/schemas 의 Pydantic 모델이 정의한다.

RAG 지식베이스(rag_documents)는 app/rag 가 원시 SQL로 관리하므로 여기 없다.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
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
    # 관리자 여부 — 인가의 서버 속성(원천). 문자열 화이트리스트가 아니라 이 값으로 게이트한다
    # (RPA-118). ADMIN_EMAILS는 이 값을 세팅하는 부트스트랩 시드로만 쓴다.
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    """갱신 토큰 (RPA-200) — 액세스 토큰이 만료돼도 재로그인 없이 이어가게 한다.

    **원문이 아니라 해시만 저장한다**: DB가 유출돼도 세션을 만들 수 없어야 한다(비밀번호와 같은 원칙).
    스테이트리스 JWT 대신 테이블을 두는 이유는 **폐기**다 — 로그아웃과 탈취 대응이 "클라이언트가
    지웠다"는 주장에 그치지 않으려면 서버가 무효를 알아야 한다.

    회전(rotation): 갱신할 때마다 옛 토큰을 폐기하고 새로 발급한다. 그래서 **이미 폐기된 토큰이
    다시 제시되면 탈취 신호**로 보고 그 사용자의 토큰 전체를 폐기한다(OAuth 2.0 Security BCP).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 한 번의 로그인에서 회전으로 이어지는 토큰들의 계열 식별자.
    # 로그아웃·탈취 대응은 **계열 단위**여야 한다: 로그아웃한 토큰만 끊으면 경합 중 회전으로
    # 발급된 후손이 살아남고(사용자는 204를 받았는데 세션이 유지된다), 반대로 사용자의 토큰을
    # 전부 끊으면 무관한 다른 기기까지 로그아웃된다.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # SHA-256 hex(64자). unique — 같은 토큰이 두 행으로 갈리면 폐기가 한쪽만 걸린다.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 폐기 시각 — NULL이면 유효. 행을 지우지 않는 이유: 재사용 탐지가 "폐기된 걸 또 냈다"를 봐야 한다.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # 자동화 대상 솔루션. 에이전트가 탈 그래프와 RAG 카탈로그를 가르는 결정론적 키
    # (업로드 시점에 확정 저장, 매 턴 그대로 에이전트에 전달 — 프롬프트로 추측 금지).
    # 현재는 A360 단일이라 기본 "a360"; 타 솔루션 카탈로그 지원은 후속.
    solution: Mapped[str] = mapped_column(String(50), server_default="a360", default="a360")
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


class AssuranceReceipt(Base):
    """보증 판정의 append-only 증거 영수증 (RPA-182).

    일반 관측 로그와 달리 향후 latest/export 차단 판단에 사용될 권위 후보이므로 제품 DB에
    저장한다. 추천·세션 FK를 의도적으로 두지 않는다. 사용자가 세션을 삭제해도 원문 payload나
    PII 없이 digest와 판정 이력은 보존되어야 하기 때문이다. UPDATE/DELETE는 DB trigger가 막는다.
    """

    __tablename__ = "assurance_receipts"
    __table_args__ = (
        CheckConstraint("harness IN ('change', 'output')", name="ck_assurance_receipts_harness"),
        CheckConstraint(
            "completeness_status IN ('complete', 'incomplete')",
            name="ck_assurance_receipts_completeness",
        ),
        CheckConstraint(
            "decision IN ('allow_candidate', 'deny', 'unassured')",
            name="ck_assurance_receipts_decision",
        ),
        CheckConstraint(
            "assurance_verdict IN ('observed', 'deny', 'refused')",
            name="ck_assurance_receipts_verdict",
        ),
        CheckConstraint(
            "rollout_mode IN ('observe', 'warn', 'enforce')",
            name="ck_assurance_receipts_rollout",
        ),
        CheckConstraint(
            "enforcement_effect IN ('none', 'warned', 'blocked')",
            name="ck_assurance_receipts_effect",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    receipt_digest: Mapped[str] = mapped_column(String(71), unique=True)
    schema_version: Mapped[str] = mapped_column(String(20))
    harness: Mapped[str] = mapped_column(String(20), index=True)  # change | output
    record_kind: Mapped[str] = mapped_column(String(40))
    writer_authority: Mapped[str] = mapped_column(String(60))
    source: Mapped[str | None] = mapped_column(String(20))
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    recommendation_version: Mapped[int | None] = mapped_column(Integer)
    candidate_id: Mapped[str | None] = mapped_column(String(71), index=True)
    payload_digest: Mapped[str | None] = mapped_column(String(71))
    source_observation_id: Mapped[str | None] = mapped_column(String(71))
    evidence_valid: Mapped[bool] = mapped_column(Boolean)
    completeness_status: Mapped[str] = mapped_column(String(20))  # complete | incomplete
    decision: Mapped[str] = mapped_column(String(30), index=True)
    assurance_verdict: Mapped[str] = mapped_column(String(20), index=True)
    assurance_status: Mapped[str] = mapped_column(String(40))
    rollout_mode: Mapped[str] = mapped_column(String(20))
    enforcement_effect: Mapped[str] = mapped_column(String(20))
    business_persisted: Mapped[bool | None] = mapped_column(Boolean)
    validator_version: Mapped[str | None] = mapped_column(String(100))
    policy_digest: Mapped[str | None] = mapped_column(String(71))
    catalog_digest: Mapped[str | None] = mapped_column(String(71))
    requested_agent_version: Mapped[str | None] = mapped_column(String(50))
    resolved_agent_version: Mapped[str | None] = mapped_column(String(50))
    receipt_payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


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


class SessionCompact(Base):
    """대화 압축본 (RPA-66). 대화가 길어지면 압축 노드가 이력을 고정 섹션 JSON으로 요약한다.

    매 턴 최신 압축본을 컨텍스트로 주입하고, 압축 시점 이후 대화만 이력으로 넘긴다(그 이전은
    이 압축본이 대체). 재압축을 거듭해도 결정사항·사용자 제공 카탈로그(verbatim)가 유실되지
    않도록 에이전트가 코드로 보정한다. append-only — 최신 행을 사용한다.
    """

    __tablename__ = "session_compacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    # {task_overview, decisions[], flow_journal[], open_questions[], verbatim[{kind, content}]}
    payload: Mapped[dict] = mapped_column(JSONB)
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
    # 입력 중 프롬프트 캐시 적중분 (RPA-199) — 캐시 입력은 정가의 10%라 비용식이 갈린다.
    # NULL=측정 전 기록(런타임 값이라 소급 복원 불가 — request_id와 동일), 0=측정했고 캐시 없음.
    # 이 구분이 있어야 실청구서 대사에서 "모르는 구간"을 정직하게 표시할 수 있다.
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # 같은 요청(턴)을 묶는 키 (RPA-158) — audit_logs·turn_events·rag_events와 동일 request_id로
    # 조인해 "이 턴이 얼마 들었나"를 재구성한다. 없으면(백그라운드 등) NULL.
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)
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


class AuditLog(Base):
    """감사 로그 (횡단 관심사/AOP) — 변경성 요청(POST/PUT/PATCH/DELETE)의 '누가·무엇을'.

    관측성 미들웨어가 자동 기록한다. 조회(GET)는 구조화 로그로만 남고 여기엔 안 들어온다
    (중요 이벤트만 DB). user_id는 JWT에서 뽑으며 익명이면 NULL.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(512), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RequestMetric(Base):
    """요청 성능 메트릭 (RPA-103) — **모든** 요청(GET 포함)의 지연·상태를 관측 DB에.

    audit_logs(변경 요청 forensics, 실제 UUID 경로)와 달리 성능 집계용이라:
    - path는 **정규화**(UUID→:id)해 저장 — GROUP BY/피벗(엔드포인트별 p95)이 되게
    - FK 없음 — 관측 DB(Neon)엔 users가 없고, 순수 메트릭이라 참조 무결성 불필요
    APScheduler 일별 롤업(metrics_daily)·Streamlit 성능 대시보드의 원천이다.
    """

    __tablename__ = "request_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(255), index=True)  # 정규화된 경로 (:id)
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True  # 일별 롤업 스캔 키
    )


class MetricsDaily(Base):
    """일별 요청 성능 집계 (RPA-104) — request_metrics를 (일자×method×path)로 피벗한 롤업.

    Streamlit(별도 레포)이 raw 대신 이걸 읽는다 — 빠르고, raw는 retention으로 정리해도
    집계본은 장기 보관. APScheduler가 주기적으로 멱등 재집계(DELETE+INSERT)한다.
    """

    __tablename__ = "metrics_daily"

    day: Mapped[str] = mapped_column(Date, primary_key=True)
    method: Mapped[str] = mapped_column(String(10), primary_key=True)
    path: Mapped[str] = mapped_column(String(255), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    err_4xx: Mapped[int] = mapped_column(Integer, default=0)
    err_5xx: Mapped[int] = mapped_column(Integer, default=0)
    p50_ms: Mapped[int | None] = mapped_column(Integer)
    p95_ms: Mapped[int | None] = mapped_column(Integer)
    avg_ms: Mapped[int | None] = mapped_column(Integer)
    max_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UsageDaily(Base):
    """일별 LLM 사용량 집계 (RPA-104) — llm_usage를 (일자×component×purpose×model)로 롤업.

    비용 대시보드의 "날짜별 purpose 비용" 피벗 원천. 규칙은 MetricsDaily와 동일(멱등 재집계).
    """

    __tablename__ = "usage_daily"

    day: Mapped[str] = mapped_column(Date, primary_key=True)
    component: Mapped[str] = mapped_column(String(30), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(50), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TurnEvent(Base):
    """에이전트 턴 노드 타임라인 (RPA-105) — /turn SSE를 지나는 stage/error/done 기록.

    백엔드가 스트림 경계에서 관측해 턴 종료 시 일괄 적재한다(에이전트는 이벤트에
    data만 얹음 — 라우트 결정·검색 쿼리·검수 위반 등). "어떤 노드를 얼마 만에 탔고
    어디서 실패했나"의 원천. token/partial은 볼륨 때문에 제외. FK 없음(관측 전용).
    """

    __tablename__ = "turn_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)  # 같은 턴 묶음 키
    seq: Mapped[int] = mapped_column(Integer)  # 턴 안 순서
    kind: Mapped[str] = mapped_column(String(10))  # stage | error | done
    stage: Mapped[str | None] = mapped_column(String(30))
    message: Mapped[str | None] = mapped_column(String(512))
    detail: Mapped[str | None] = mapped_column(Text)  # 이벤트 data JSON (route·query·violations 등)
    elapsed_ms: Mapped[int] = mapped_column(Integer)  # 턴 시작 기준 경과
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AlertState(Base):
    """알림 전이·쿨다운 상태 (RPA-189) — "이미 보냈나"를 기억한다. FK 없음(관측 전용).

    **왜 DB인가:** 인메모리로 두면 (a) 재시작마다 재알림 (b) uvicorn --workers N이면 N개
    프로세스가 각자 보낸다. "스로틀했다"고 주장하려면 그 상태를 **모든 발신자가 함께 보는
    곳**에 둬야 한다 — 가드가 읽는 것과 동작이 읽는 것이 같아야 한다(CONVENTIONS §9).

    key = 알림 종류 식별자. 같은 key의 상태가 바뀌면(ok↔firing) 전이로 보고 알린다.
      예: "budget:global:daily" · "budget:subject:e7a3929d:daily" · "health:degraded" · "metrics:5xx"
    """

    __tablename__ = "alert_state"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))  # ok | firing
    last_sent_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text)  # 마지막으로 보낸 내용(디버깅·중복 판단용)
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RagEvent(Base):
    """RAG 파이프라인 단계 로그 (RPA-128) — embed/search/rerank 단계별 소요·파라미터를
    관측 DB에 중앙화. 로컬 JSONL(app/rag/logs)과 같은 내용이되 유실 방지·중앙화·대시보드
    조회용. 검색어 preview는 마스킹(app/core/masking). detail에 RAG 설정 스냅샷(chunk_size·
    모델·RRF 등)을 담아 "이 검색이 어떤 설정으로 돌았나"를 가시화한다. FK 없음(관측 전용).
    """

    __tablename__ = "rag_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)  # 같은 검색 흐름 묶음 키
    event: Mapped[str] = mapped_column(String(40))  # embed_query|bm25_search|voyage_rerank|hybrid_search|http_request
    function: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str | None] = mapped_column(String(20))  # ok | error
    duration_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)  # JSON: args·result·config (query preview 마스킹)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RetrievalParamOverride(Base):
    """검색 파라미터 런타임 오버라이드 (RPA-149) — 무중단 튜닝의 단일 진실 공급원.

    config는 import 시점에 상수로 고정돼 값 변경 = 재시작이다. 이 테이블은 admin API가 쓰고
    검색 경로가 읽어, 재시작 없이 RRF·후보풀·가중치를 조정한다. **append-only·최신행 우선** —
    행마다 updated_by/created_at을 남겨 "누가 언제 뭘 바꿨나" 감사 이력을 겸한다. 활성값은
    가장 최근 행(id DESC). 행이 하나도 없으면 검색은 from_config(.env)로 폴백한다(로컬 무변경).
    값 검증은 app/rag/retrieval/params.py의 RetrievalParams.__post_init__을 재사용한다(단일 진실).
    """

    __tablename__ = "retrieval_params"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_pool_size: Mapped[int] = mapped_column(Integer)
    rerank_candidates: Mapped[int] = mapped_column(Integer)
    rrf_k: Mapped[int] = mapped_column(Integer)
    vector_weight: Mapped[float] = mapped_column(Float)
    bm25_weight: Mapped[float] = mapped_column(Float)
    # 변경 주체 — 사람 관리자면 이메일, ops-server(X-API-Key)면 "service". 감사용.
    updated_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class BudgetLimitOverride(Base):
    """LLM 예산 상한의 런타임 오버라이드 — 모니터링에서 재시작 없이 조정한다 (RPA-173).

    RPA-149(retrieval_params)와 **같은 패턴**: admin API가 쓰고 /turn 경로가 읽어, 재배포 없이
    상한을 바꾼다. **append-only·최신행 우선** — 행마다 updated_by/created_at을 남겨 "누가 언제
    상한을 얼마로 바꿨나" 감사 이력을 겸한다(예산은 서비스를 막는 값이라 이력이 특히 중요하다).
    행이 하나도 없으면 .env(BUDGET_*_USD)로 폴백한다 — 오버라이드 없는 배포는 무변경.

    **전부 아니면 전무**: 행이 있으면 4개 값이 통째로 env를 대체한다(일부만 오버라이드하는
    혼합 규칙은 "지금 실제 상한이 얼마냐"를 추적 불가능하게 만든다). 특정 상한만 끄려면 NULL.
    """

    __tablename__ = "budget_limits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # NULL = 그 상한 비활성 (env의 '미설정=비활성'과 같은 의미)
    subject_daily_usd: Mapped[float | None] = mapped_column(Float)
    subject_monthly_usd: Mapped[float | None] = mapped_column(Float)
    global_daily_usd: Mapped[float | None] = mapped_column(Float)
    global_monthly_usd: Mapped[float | None] = mapped_column(Float)
    updated_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
