import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


app = FastAPI(title="A360 Assistant Backend", version="0.1.0")

frontend_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

# Vercel gives every deployment a new hash-suffixed URL (e.g.
# a360-assistant-frontend-<hash>-a360-assistant.vercel.app), so a fixed
# FRONTEND_ORIGINS entry breaks on each redeploy. Allow any deployment of
# this Vercel project via regex instead of chasing the hash by hand.
frontend_origin_regex = os.getenv(
    "FRONTEND_ORIGIN_REGEX",
    r"https://a360-assistant-frontend-.*-a360-assistant\.vercel\.app",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_origin_regex=frontend_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EchoRequest(BaseModel):
    message: str


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "A360 Assistant Backend",
        "environment": os.getenv("APP_ENV", "development"),
        "status": "ok",
    }


@app.get("/api/message")
def message() -> dict[str, str]:
    return {
        "service": "A360 Assistant Backend",
        "environment": os.getenv("APP_ENV", "development"),
        "message": "Frontend and backend are connected.",
    }


@app.post("/api/echo")
def echo(payload: EchoRequest) -> dict[str, str]:
    return {
        "received": payload.message,
        "reply": f"Backend received: {payload.message}",
    }


@app.get("/api/health")
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/api/rag/search")
def rag_search(q: str, limit: int = 5) -> dict:
    """A360 패키지/액션 지식 벡터 검색. app/ingest 파이프라인으로 적재된 데이터를 사용한다."""
    from app.ingest import db
    from app.ingest.embed import embed_query

    try:
        query_embedding = embed_query(q)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"임베딩 설정 오류: {e}")

    try:
        conn = db.connect()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {e}")
    try:
        results = db.search(conn, query_embedding, limit=limit)
    finally:
        conn.close()
    return {"query": q, "results": results}
