import os
import re
from pathlib import Path

# postgres/postgresql 스킴 뒤의 SQLAlchemy 드라이버 접미사(+psycopg, +psycopg2 등)를 벗긴다.
_PG_DRIVER_SUFFIX = re.compile(r"^(postgres(?:ql)?)\+\w+://")

# 프로젝트 루트의 .env를 있으면 로드 (python-dotenv 없거나 파일 없으면 조용히 통과)
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

DOCS_BASE_URL = os.getenv("AA_DOCS_BASE_URL", "https://docs.automationanywhere.com")

DATA_DIR = Path(os.getenv("INGEST_DATA_DIR", "data/ingest"))
DOCS_JSONL = DATA_DIR / "docs.jsonl"  # 기본 로케일(ko-KR) — 서비스에 실제 쓰이는 본진 콘텐츠


def docs_jsonl_for_locale(locale: str) -> Path:
    """로케일별 문서 크롤 결과 경로. ko-KR은 기존 DOCS_JSONL 그대로(하위호환), 그 외
    로케일(en-US 등)은 별 파일로 — 동시 크롤 시 같은 파일에 동시쓰기해서 깨지는 걸 방지하고,
    en-US는 action_name 매칭 보조용일 뿐 서비스 콘텐츠로 이중 적재하지 않을 것이므로 구분한다."""
    if locale == "ko-KR":
        return DOCS_JSONL
    return DATA_DIR / f"docs_{locale}.jsonl"
PACKAGES_JSON = DATA_DIR / "packages.json"
BOTS_JSONL = DATA_DIR / "bots.jsonl"
EXPORTS_DIR = DATA_DIR / "exports"
RAG_DOCUMENTS_JSONL = DATA_DIR / "rag_documents.jsonl"
EDA_REPORT_JSON = DATA_DIR / "eda_report.json"
# doc_action_tree 트리 해석 결과 요약(패키지별 리프/카테고리 수) — export-for-agent
# 실행마다 갱신되는 감사용 사이드카 파일. build가 읽지 않음, 사람이 검토하는 용도.
DOC_ACTION_TREE_REPORT_JSON = DATA_DIR / "doc_action_tree_report.json"
# 패키지 판별 + 메뉴 계층(루트/카테고리/리프, JAR 유무와 무관하게 전체) 확정된 구조를
# 그대로 남기는 산출물 — build-action-tree 산출, JAR/Agent 어느 쪽도 없이도 "이
# 패키지엔 이런 하위 구조가 있다"를 바로 확인 가능.
PACKAGE_ACTION_TREE_JSON = DATA_DIR / "package_action_tree.json"
# JAR이 없는 패키지들의 리프 문서(구조화 HTML 포함)를, "이 리프가 진짜 액션인지"를
# 판단할 향후 LLM 기반 파싱 Agent에게 그대로 넘기기 위한 산출물
# (export-for-agent 산출, app/rag/pipeline.py::cmd_export_for_agent 참고).
AGENT_HANDOFF_JSONL = DATA_DIR / "agent_handoff.jsonl"
# 리프=진짜 액션 여부를 필터링하지 않고 전부 액션 후보로 나열하는 단순 베이스라인
# (export-naive-leaf-actions 산출, app/rag/build/naive_leaf_actions.py 참고).
# 파라미터 스키마 없음 — action_schema로 쓰지 않음, merge.py가 조회하지 않음.
NAIVE_LEAF_ACTIONS_JSONL = DATA_DIR / "naive_leaf_actions.jsonl"

# 검색/리랭커 파이프라인 각 단계 로그 (JSON Lines, 날짜별 파일) — observability.py가 씀
LOG_DIR = Path(os.getenv("RAG_LOG_DIR") or "app/rag/logs")

# 청킹: chunk_size 초과 문서만 분할한다. 기본값은 NongSabu DocumentChunker 프라이어(1200/200) —
# `pipeline.py eda`로 실제 문서 길이 분포를 확인한 뒤 필요시 .env에서 조정한다.
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST") or "http://localhost:9200"
OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "rag_documents")
OPENSEARCH_USERNAME = os.getenv("OPENSEARCH_USERNAME", "")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "")

# 하이브리드 검색(RRF) + Voyage Reranker
RRF_K = int(os.getenv("RRF_K", "60"))
HYBRID_CANDIDATE_POOL_SIZE = int(os.getenv("HYBRID_CANDIDATE_POOL_SIZE", "50"))
HYBRID_RERANK_CANDIDATES = int(os.getenv("HYBRID_RERANK_CANDIDATES", "20"))
# RRF branch 가중치 — 벡터(의미)/BM25(키워드) 신호 비중 조절 (기본 1.0=동일 가중, RPA-147).
RRF_VECTOR_WEIGHT = float(os.getenv("RRF_VECTOR_WEIGHT", "1.0"))
RRF_BM25_WEIGHT = float(os.getenv("RRF_BM25_WEIGHT", "1.0"))
RERANK_MODEL = os.getenv("RERANK_MODEL", "rerank-2.5-lite")

# voyage(기본) 또는 openai. Anthropic은 임베딩 API가 없어 Voyage AI를 공식 권장함.
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "voyage")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "voyage-3.5" if EMBEDDING_PROVIDER == "voyage" else "text-embedding-3-small",
)
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024" if EMBEDDING_PROVIDER == "voyage" else "1536"))
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class RagDatabaseConfigurationError(RuntimeError):
    """RAG 전용 DB 설정이 없어 서비스 DB 격리를 보장할 수 없음."""


def database_dsn() -> str:
    """RAG 저장소(pgvector) 접속 문자열.

    RAG_DATABASE_URL만 사용한다. 미설정/빈값이면 기동을 거부해 RAG 코퍼스가 서비스 DB에
    조용히 섞이는 구성을 막는다. 로컬도 docker-compose가 별도 논리 DB URL을 명시한다.
    """
    url = (os.getenv("RAG_DATABASE_URL") or "").strip()
    if url:
        # RAG store는 raw psycopg라 libpq URL(postgresql://)만 받는다 — SQLAlchemy용
        # 'postgresql+psycopg://' 접두사를 그대로 넘기면 psycopg가 스킴을 못 읽는다.
        # 관측 URL 형식을 복붙해도 동작하도록 드라이버 접미사(+psycopg 등)를 방어적으로 벗긴다.
        return _PG_DRIVER_SUFFIX.sub(r"\1://", url, count=1)
    raise RagDatabaseConfigurationError("RAG_DATABASE_URL is required")
