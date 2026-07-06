import os
from pathlib import Path

# 프로젝트 루트의 .env를 있으면 로드 (python-dotenv 없거나 파일 없으면 조용히 통과)
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

DOCS_BASE_URL = os.getenv("AA_DOCS_BASE_URL", "https://docs.automationanywhere.com")

DATA_DIR = Path(os.getenv("INGEST_DATA_DIR", "data/ingest"))
DOCS_JSONL = DATA_DIR / "docs.jsonl"
PACKAGES_JSON = DATA_DIR / "packages.json"
BOTS_JSONL = DATA_DIR / "bots.jsonl"
EXPORTS_DIR = DATA_DIR / "exports"
RAG_DOCUMENTS_JSONL = DATA_DIR / "rag_documents.jsonl"
EDA_REPORT_JSON = DATA_DIR / "eda_report.json"

# 청킹: chunk_size 초과 문서만 분할한다. 기본값은 NongSabu DocumentChunker 프라이어(1200/200) —
# `pipeline.py eda`로 실제 문서 길이 분포를 확인한 뒤 필요시 .env에서 조정한다.
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

# voyage(기본) 또는 openai. Anthropic은 임베딩 API가 없어 Voyage AI를 공식 권장함.
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "voyage")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "voyage-3.5" if EMBEDDING_PROVIDER == "voyage" else "text-embedding-3-small",
)
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024" if EMBEDDING_PROVIDER == "voyage" else "1536"))
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def database_dsn() -> str:
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "a360")
    user = os.getenv("DATABASE_USERNAME", "a360_admin")
    password = os.getenv("DATABASE_PASSWORD", "a360_local_password")
    return f"host={host} port={port} dbname={name} user={user} password={password}"
