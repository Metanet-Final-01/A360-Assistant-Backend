"""중앙 설정 레지스트리 (RPA-224) — 백엔드의 모든 환경변수를 여기 **선언**한다.

배경: os.getenv가 26개 파일·75개 키로 산재해 "무슨 설정이 있는지"를 아무도 한눈에 못
봤고, 산재된 조용한 폴백이 실제 사고를 냈다(RPA-160: OPENSEARCH_HOST 빈 값 → localhost
폴백 → BM25 무음 사망). 이 파일이 그 목록의 단일 진실 공급원이다 — 새 환경변수는
여기 선언부터 한다 (tests/test_config_registry.py 래칫이 강제한다).

⚠️ **pydantic BaseSettings(임포트 시점 고정)를 일부러 쓰지 않았다.** 이 레포는 여러
곳이 의도적으로 **호출 시점에** env를 읽는다:
  - APP_DATABASE_URL 토글(RPA-186) — run_migrations는 호출 시점 env 계약 (db.py docstring)
  - OBSERVABILITY_DATABASE_URL — conftest가 테스트별 setenv로 공유 Neon 오염을 막는다
  - 통합 테스트의 DATABASE_NAME 전환(a360_test), test_alerts의 SLACK_WEBHOOK_URL 등
임포트 시점에 값을 얼리면 이 계약과 테스트 격리가 전부 깨진다. 그래서 **선언은 중앙,
읽기는 접근 시점**이다 — `config.JWT_SECRET`은 접근할 때마다 os.getenv를 탄다.

사용법:
    from app.core import config
    secret = config.JWT_SECRET          # str (없으면 선언된 기본값)
    days = config.REFRESH_TOKEN_EXPIRE_DAYS  # 선언된 cast(int)가 적용된 값

값이 아니라 선언이 필요하면 REGISTRY를 직접 본다 (관리 화면·문서 생성·래칫 테스트).
"""

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvSpec:
    """환경변수 한 개의 선언.

    - default: 미설정 시 값 (None = 기본값 없음 — 호출부가 부재를 직접 다룬다)
    - cast: 접근 시 적용할 변환 (str/int/float/bool)
    - secret: 값이 크레덴셜 — 로그·화면에 절대 출력 금지 표시
    - dynamic: 호출 시점 읽기가 **계약**인 키 (토글·테스트 격리가 의존) — 어떤 형태로든
      임포트 시점 캐시로 바꾸면 안 된다
    - warn_if_unset: 미설정이 조용한 저하를 부르는 키 — startup_report()가 경고
    """
    default: str | None
    cast: type = str
    group: str = ""
    doc: str = ""
    secret: bool = False
    dynamic: bool = False
    warn_if_unset: bool = False


def _bool(v: str) -> bool:
    """명시적 false 값만 False, 나머지(오타 포함)는 True — fail-secure (Qodo 반영).

    보안 토글(SECURE_COOKIES)은 오타 하나가 secure를 꺼선 안 된다. 기존
    auth._cookie_security와 **같은 규칙**(unknown→on)으로 맞춘다 — truthy 화이트리스트
    (unknown→off)는 정반대라, 레지스트리 값과 실제 동작이 갈렸다.
    """
    return v.strip().lower() not in ("false", "0", "no", "off")


# 그룹 순서: DB → 인증 → LLM → RAG → 관측/알림 → 업로드/파서 → 앱/디버그
REGISTRY: dict[str, EnvSpec] = {
    # --- DB ---
    "APP_DATABASE_URL": EnvSpec("", group="db", secret=True, dynamic=True,
        doc="공유 앱 DB(Neon). 설정 시 DATABASE_* 조각을 통째로 대체 (RPA-186 토글)"),
    "DATABASE_HOST": EnvSpec("localhost", group="db", dynamic=True, doc="로컬/프로덕션 앱 DB 호스트"),
    "DATABASE_PORT": EnvSpec("5432", group="db", dynamic=True, doc="앱 DB 포트 (로컬 docker는 5433)"),
    "DATABASE_NAME": EnvSpec("a360", group="db", dynamic=True,
        doc="앱 DB 이름. 통합 테스트가 호출 시점에 a360_test로 전환한다"),
    "DATABASE_USERNAME": EnvSpec("a360_admin", group="db", dynamic=True, doc="앱 DB 사용자"),
    "DATABASE_PASSWORD": EnvSpec("", group="db", secret=True, dynamic=True, doc="앱 DB 비밀번호"),
    "OBSERVABILITY_DATABASE_URL": EnvSpec("", group="db", secret=True, dynamic=True,
        doc="관측 전용 공유 DB(RPA-90). 미설정 시 앱 DB 폴백. conftest가 테스트별로 격리"),
    "RAG_DATABASE_URL": EnvSpec(None, group="db", secret=True, dynamic=True,
        doc="RAG 코퍼스 DB(RPA-132). 미설정 시 DATABASE_* 폴백"),

    # --- 인증/보안 ---
    "JWT_SECRET": EnvSpec("", group="auth", secret=True, warn_if_unset=True,
        doc="JWT 서명 키. 프로덕션 미설정이면 security.py가 기동을 거부한다"),
    "ACCESS_TOKEN_EXPIRE_MINUTES": EnvSpec("60", cast=int, group="auth", doc="액세스 토큰 만료(분)"),
    "REFRESH_TOKEN_EXPIRE_DAYS": EnvSpec("14", cast=int, group="auth", doc="리프레시 토큰 만료(일) (RPA-200)"),
    "REFRESH_REUSE_GRACE_SECONDS": EnvSpec("10", cast=int, group="auth",
        doc="폐기된 리프레시 토큰 재사용 허용 유예(초) — 네트워크 재시도 오탐 방지"),
    "SECURE_COOKIES": EnvSpec("true", cast=_bool, group="auth",
        doc="리프레시 쿠키 Secure 속성. 로컬 http 개발에서만 false"),
    "ADMIN_EMAILS": EnvSpec("", group="auth",
        doc="관리자 시드 계정(콤마 구분) — 기동 시 is_admin 백필 (RPA-118)"),
    "OPS_API_KEY": EnvSpec("", group="auth", secret=True,
        doc="백오피스 admin API 키. 미설정이면 admin API 전체 거부"),
    "ASSURANCE_WRITER_TOKEN": EnvSpec("", group="auth", secret=True,
        doc="Change Assurance 기록 쓰기 전용 토큰 (RPA-217)"),
    "ASSURANCE_WRITER_REPOSITORY": EnvSpec("", group="auth",
        doc="Assurance 기록을 게시할 수 있는 유일한 GitHub 레포"),

    # --- LLM ---
    "OPENAI_API_KEY": EnvSpec("", group="llm", secret=True, warn_if_unset=True,
        doc="OpenAI API 키. ⚠️ Windows Machine env가 .env를 가리는 함정 이력 있음"),
    "OPENAI_MODEL": EnvSpec("gpt-5.4-mini", group="llm", doc="기본 LLM 모델"),
    "MAX_LLM_CONCURRENCY": EnvSpec("3", cast=int, group="llm", doc="에이전트 LLM 동시 호출 상한"),
    "LLM_TIMEOUT_SECONDS": EnvSpec("180.0", cast=float, group="llm",
        doc="LLM 호출 전체 타임아웃(초) (RPA-202)"),
    "LLM_CONNECT_TIMEOUT_SECONDS": EnvSpec("5.0", cast=float, group="llm", doc="LLM 연결 타임아웃(초)"),
    "LLM_MAX_RETRIES": EnvSpec("2", cast=int, group="llm", doc="LLM 재시도 횟수"),
    "AGENT_VERSION": EnvSpec(None, group="llm", doc="에이전트 그래프 버전 선택 (v1/v2/v3, 미설정=기본)"),
    # LLM 단가(1M 토큰당 USD) — os.environ[]로 읽는 보조 모델 폴백 단가 (llm.py). 미설정 시
    # None → 비용 계산 생략(기존 동작). RPA-224 래칫이 environ[]를 못 잡아 누락됐던 키들.
    "LLM_INPUT_COST_PER_1M": EnvSpec(None, cast=float, group="llm", doc="입력 토큰 1M당 USD"),
    "LLM_OUTPUT_COST_PER_1M": EnvSpec(None, cast=float, group="llm", doc="출력 토큰 1M당 USD"),
    "LLM_CACHED_INPUT_COST_PER_1M": EnvSpec(None, cast=float, group="llm", doc="캐시 입력 토큰 1M당 USD"),

    # --- RAG ---
    "AA_DOCS_BASE_URL": EnvSpec("https://docs.automationanywhere.com", group="rag", doc="AA 문서 원본 베이스 URL"),
    "INGEST_DATA_DIR": EnvSpec("data/ingest", group="rag", doc="수집 산출물 디렉터리"),
    "RAG_LOG_DIR": EnvSpec(None, group="rag", doc="rag_events JSONL 사본 디렉터리 (미설정=기본 경로)"),
    "CHUNK_SIZE": EnvSpec("1200", cast=int, group="rag", doc="청크 크기(문자)"),
    "CHUNK_OVERLAP": EnvSpec("200", cast=int, group="rag", doc="청크 오버랩(문자)"),
    "OPENSEARCH_HOST": EnvSpec(None, group="rag", warn_if_unset=True,
        doc="BM25 OpenSearch 호스트. ⚠️ 빈 값 → localhost 폴백으로 무음 사망한 사고(RPA-160)"),
    "OPENSEARCH_INDEX": EnvSpec("rag_documents", group="rag", doc="BM25 인덱스명"),
    "OPENSEARCH_USERNAME": EnvSpec("", group="rag", doc="OpenSearch 사용자"),
    "OPENSEARCH_PASSWORD": EnvSpec("", group="rag", secret=True, doc="OpenSearch 비밀번호"),
    "RRF_K": EnvSpec("60", cast=int, group="rag", doc="RRF 상수 k (RPA-130)"),
    "HYBRID_CANDIDATE_POOL_SIZE": EnvSpec("50", cast=int, group="rag", doc="하이브리드 후보 풀 크기"),
    "HYBRID_RERANK_CANDIDATES": EnvSpec("20", cast=int, group="rag", doc="리랭크 후보 수"),
    "RRF_VECTOR_WEIGHT": EnvSpec("1.0", cast=float, group="rag", doc="RRF dense 가중치"),
    "RRF_BM25_WEIGHT": EnvSpec("1.0", cast=float, group="rag", doc="RRF BM25 가중치"),
    "RERANK_MODEL": EnvSpec("rerank-2.5-lite", group="rag", doc="Voyage 리랭커 모델"),
    "EMBEDDING_PROVIDER": EnvSpec("voyage", group="rag", doc="임베딩 공급자 (voyage/openai)"),
    "EMBEDDING_DIM": EnvSpec(None, group="rag", cast=int,
        doc="임베딩 차원. 미설정 시 공급자별 기본(voyage=1024, openai=1536) — rag/config.py가 결정"),
    "EMBEDDING_MODEL": EnvSpec(None, group="rag", doc="임베딩 모델 오버라이드"),
    "VOYAGE_API_KEY": EnvSpec("", group="rag", secret=True, doc="Voyage API 키 (임베딩·리랭크)"),
    "RAG_EVENT_QUEUE": EnvSpec("", group="rag", dynamic=True,
        doc="rag_events 배치 큐 토글 (RPA-221). 테스트가 동기 모드로 전환"),
    "RAG_CACHE_ENABLED": EnvSpec("", group="rag", dynamic=True, doc="RAG 검색 캐시 토글 (RPA-211)"),
    "RAG_CACHE_TTL_SECONDS": EnvSpec("3600", cast=int, group="rag", doc="검색 캐시 TTL(초)"),
    "RAG_CACHE_MAXSIZE": EnvSpec("2048", cast=int, group="rag", doc="검색 캐시 항목 상한"),
    "GITHUB_TOKEN": EnvSpec(None, group="rag", secret=True, doc="패키지 JAR 수집용 GitHub 토큰"),
    "CR_URL": EnvSpec("", group="rag", doc="Control Room URL (패키지 수집)"),
    "CR_USERNAME": EnvSpec("", group="rag", doc="Control Room 사용자"),
    "CR_API_KEY": EnvSpec("", group="rag", secret=True, doc="Control Room API 키"),
    "CR_PASSWORD": EnvSpec("", group="rag", secret=True, doc="Control Room 비밀번호"),

    # --- 관측/알림 ---
    "METRICS_ROLLUP_ENABLED": EnvSpec("true", cast=_bool, group="obs", doc="일별 롤업 스케줄러 토글 (RPA-104)"),
    "ROLLUP_INTERVAL_MINUTES": EnvSpec("60", cast=int, group="obs", doc="롤업 주기(분)"),
    "ALERT_HEALTH_INTERVAL_MINUTES": EnvSpec("5", cast=int, group="obs", doc="헬스 경보 판정 주기(분)"),
    "SLACK_WEBHOOK_URL": EnvSpec("", group="obs", secret=True, dynamic=True,
        doc="Slack 경보 웹훅 (RPA-189). 미설정=경보 비활성"),
    "ALERT_COOLDOWN_MINUTES": EnvSpec("60", cast=int, group="obs", dynamic=True,
        doc="같은 사유 경보 재발송 쿨다운(분)"),
    "TURN_GAUGE_WARN_RATIO": EnvSpec("0.87", cast=float, group="obs", doc="턴 토큰 게이지 경고 비율"),
    "TURN_GAUGE_HARD_RATIO": EnvSpec("1.0", cast=float, group="obs", doc="턴 토큰 게이지 차단 비율"),
    "TURN_GAUGE_LIMIT_TOKENS": EnvSpec("6000", cast=int, group="obs", doc="턴 토큰 게이지 상한"),
    "TURN_MAX_DURATION_SEC": EnvSpec("900", cast=float, group="obs",
                                     doc="턴 전체 상한(초) — 초과 시 hung으로 보고 끊는다, 0이면 끔 (RPA-235)"),
    # 보존 정책(일) — rollup.py가 _RETENTION 테이블의 키를 os.getenv(변수)로 읽는다.
    "METRICS_RETENTION_DAYS": EnvSpec("30", cast=int, group="obs", doc="request_metrics 보존일"),
    "TURN_EVENTS_RETENTION_DAYS": EnvSpec("30", cast=int, group="obs", doc="turn_events 보존일"),
    "LLM_USAGE_RETENTION_DAYS": EnvSpec("90", cast=int, group="obs", doc="llm_usage 보존일"),
    "AUDIT_RETENTION_DAYS": EnvSpec("365", cast=int, group="obs", doc="audit_logs 보존일"),
    # 알림 임계 — alerts.py가 _threshold(name)로 읽는다. 미설정 시 그 알림 비활성.
    "ALERT_GLOBAL_DAILY_USD": EnvSpec(None, cast=float, group="obs", doc="일일 전역 비용 알림 임계(USD)"),
    "ALERT_5XX_DAILY": EnvSpec(None, cast=float, group="obs", doc="일일 5xx 건수 알림 임계"),
    # 예산 상한 — budget.py가 _limit(name)로 읽는다. 미설정 시 그 상한 비활성(fail-open).
    "BUDGET_SUBJECT_DAILY_USD": EnvSpec(None, cast=float, group="obs", doc="주체별 일 예산 상한(USD)"),
    "BUDGET_SUBJECT_MONTHLY_USD": EnvSpec(None, cast=float, group="obs", doc="주체별 월 예산 상한(USD)"),
    "BUDGET_GLOBAL_DAILY_USD": EnvSpec(None, cast=float, group="obs", doc="전역 일 예산 상한(USD)"),
    "BUDGET_GLOBAL_MONTHLY_USD": EnvSpec(None, cast=float, group="obs", doc="전역 월 예산 상한(USD)"),

    # --- 업로드/파서 ---
    "DOCUMENT_BUCKET": EnvSpec("", group="upload", warn_if_unset=True,
        doc="업로드 S3 버킷. ⚠️ 미설정이면 로컬 폴백 — 다중 인스턴스에서 파일 유실로 보임"),
    "UPLOAD_DIR": EnvSpec("data/uploads", group="upload", doc="로컬 폴백 업로드 디렉터리"),
    "MAX_UPLOAD_MB": EnvSpec("20", cast=int, group="upload", doc="업로드 크기 상한(MB)"),
    "PDFBOX_JAR_PATH": EnvSpec("", group="parser", doc="PDFBox JAR 경로 (PDF 파서)"),
    "LIBREOFFICE_PATH": EnvSpec("", group="parser", doc="LibreOffice 실행 파일 경로 (PPT 파서)"),
    "VISION_MIN_TEXT_CHARS": EnvSpec("200", cast=int, group="parser", doc="비전 파서 발동 텍스트 임계"),
    "VISION_MAX_PAGES": EnvSpec("15", cast=int, group="parser", doc="비전 파서 페이지 상한"),
    "VISION_FORCE_EMPTY_CHARS": EnvSpec("50", cast=int, group="parser", doc="빈 페이지 강제 비전 임계"),
    "VISION_MODEL": EnvSpec("", group="parser", doc="비전 모델 오버라이드"),
    "VISION_CONCURRENCY": EnvSpec("4", cast=int, group="parser", doc="비전 호출 동시성"),

    # --- 앱/디버그 ---
    "APP_ENV": EnvSpec("development", group="app", doc="환경 구분 (development/production)"),
    "FRONTEND_ORIGINS": EnvSpec("http://localhost:5173,http://127.0.0.1:5173", group="app",
        doc="CORS 허용 origin (콤마 구분)"),
    "FRONTEND_ORIGIN_REGEX": EnvSpec(r"https://a360-assistant-frontend-.*-a360-assistant\.vercel\.app",
        group="app", doc="Vercel 해시 배포 URL 허용 정규식"),
    "DEBUG_ENDPOINTS_ENABLED": EnvSpec("", group="debug", doc="디버그 엔드포인트 토글"),
    "DEBUG_HTTP_CLIENT_ENABLED": EnvSpec("", group="debug", doc="디버그 HTTP 클라이언트 토글"),
}


def get(key: str):
    """선언된 키의 현재 값 — **접근 시점**에 os.getenv를 읽고 선언된 cast를 적용한다.

    미선언 키는 KeyError — os.getenv처럼 조용히 None을 주지 않는다. 오타가 "설정이
    안 먹네"가 아니라 즉시 예외로 드러나게 하기 위해서다(RPA-160류 무음 폴백 방지).
    """
    spec = REGISTRY[key]
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        # 빈 문자열·**공백만인 값**을 "미설정"으로 본다 — startup_report()가 .strip 기준으로
        # 판정하므로 여기도 같아야 한다(Qodo): 안 그러면 기동 로그는 '미설정' 경고인데
        # 런타임은 공백을 그대로 cast해 int("   ")로 터진다. 기존 산재 호출부의 `or` 폴백
        # 의미론(예: OPENSEARCH_HOST)과도 일치한다.
        if spec.default is None:
            return None
        raw = spec.default
    if raw == "" and spec.cast is not str:
        return None  # 빈 기본값에 숫자 cast를 적용하면 ValueError — 부재는 None으로
    return spec.cast(raw)


def __getattr__(name: str):
    """모듈 속성 접근(config.JWT_SECRET)을 레지스트리 조회로 연결한다 (PEP 562)."""
    if name in REGISTRY:
        return get(name)
    raise AttributeError(f"미선언 환경변수: {name} — app/core/config.py REGISTRY에 먼저 선언하세요")


def startup_report() -> list[str]:
    """warn_if_unset 키 중 미설정인 것을 경고 로그로 남기고 목록을 돌려준다.

    기동 시 한 번 호출된다(main.py lifespan). 값을 바꾸지 않는다 — RPA-160(무음 폴백
    사고)의 처방은 "폴백을 없애라"가 아니라 "폴백이 **보이게** 하라"다. 로컬 개발은
    이 키들이 비어 있는 게 정상이라 경고 수준으로만 남긴다.
    """
    missing = [k for k, s in REGISTRY.items() if s.warn_if_unset and not (os.getenv(k) or "").strip()]
    for k in missing:
        logger.warning("환경변수 미설정: %s — %s", k, REGISTRY[k].doc)
    return missing
