"""관측 전용 DB 엔진 — audit_logs·llm_usage를 팀 공유 Postgres로 분리한다 (RPA-90).

로그가 각자 로컬 DB에 격리되면 팀 단위 비용·감사 집계가 불가능하다. 관측 데이터만
별도 공유 DB(Neon → 추후 AWS RDS)로 보내고, 앱 DB(rag_documents·pgvector 등)는
각자 로컬을 유지한다 (wiki 의사결정-기록 2026-07-10 "관측 로그, 왜 수집하나").

- OBSERVABILITY_DATABASE_URL 설정 시: 관측 쓰기/읽기가 이 엔진으로 간다.
- 미설정 시: 앱 DB(app.db.SessionLocal)로 폴백 — 로컬 단독 개발은 기존과 동일.
- 관측 DB 장애는 호출자를 실패시키지 않는다 (쓰기는 전부 best-effort).

관측 DB의 테이블은 앱 스키마의 FK(users·analysis_sessions)를 만족할 수 없으므로,
FK 제약을 뗀 동형 테이블을 만든다 — 컬럼·인덱스는 models와 동일해 ORM 객체를
그대로 add/select할 수 있다.
"""

import logging
import os
import threading

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import ForeignKeyConstraint

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_engine = None
_sessionmaker = None
_url_cached: str | None = None


def observability_url() -> str:
    return os.getenv("OBSERVABILITY_DATABASE_URL", "").strip()


def _build(url: str):
    """관측 엔진·세션 팩토리를 만든다. pool_pre_ping — Neon scale-to-zero 후 죽은 커넥션 정리."""
    engine = create_engine(url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def observability_sessionmaker():
    """관측 세션 팩토리 — URL 설정 시 관측 엔진, 미설정 시 앱 SessionLocal 폴백.

    폴백은 호출 시점에 app.db 모듈 속성을 참조한다 — 테스트의
    monkeypatch("app.db.SessionLocal")이 그대로 효과를 갖는 계약(기존 테스트 호환).
    URL이 바뀌면(테스트 setenv 등) 엔진을 재생성한다.
    """
    global _engine, _sessionmaker, _url_cached
    url = observability_url()
    if not url:
        import app.db as app_db

        return app_db.SessionLocal
    with _lock:
        if _sessionmaker is None or url != _url_cached:
            if _engine is not None:
                _engine.dispose()
            _engine, _sessionmaker = _build(url)
            _url_cached = url
        return _sessionmaker


def get_obs_db():
    """FastAPI 의존성: 관측 조회용 세션 (admin stats 등)."""
    db = observability_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def _observability_metadata() -> MetaData:
    """관측 테이블(audit_logs·llm_usage)의 FK-제거 사본 메타데이터.

    관측 DB엔 부모 테이블(users·analysis_sessions)이 없다 — to_metadata 사본에서
    FK를 지우는 방식은 컬럼에 남는 참조 때문에 create 시 부모를 찾다 실패하므로,
    처음부터 FK 없이 컬럼(이름·타입·PK·nullable·server_default)과 인덱스만 재구성한다.
    테이블명·컬럼이 models와 동일해 ORM 객체를 그대로 add/select할 수 있다.
    """
    from sqlalchemy import Column, Index, Table

    from app import models

    meta = MetaData()
    for src in (models.AuditLog.__table__, models.LlmUsage.__table__, models.RequestMetric.__table__):
        table = Table(
            src.name,
            meta,
            *[
                Column(
                    c.name,
                    c.type,
                    primary_key=c.primary_key,
                    nullable=c.nullable,
                    server_default=c.server_default.arg if c.server_default is not None else None,
                )
                for c in src.columns
            ],
        )
        for idx in src.indexes:
            Index(idx.name, *[table.c[col.name] for col in idx.columns], unique=idx.unique)
    return meta


def ensure_observability_schema() -> bool:
    """URL 설정 시 관측 DB에 테이블을 보장한다(checkfirst). 실패해도 앱 기동은 계속.

    관측 테이블 2개는 스키마 변경이 드물어 별도 Alembic 체인 대신 create_all을 쓴다 —
    컬럼 추가가 생기면 그때 관측 전용 마이그레이션을 도입한다(과잉 설계 방지).
    """
    url = observability_url()
    if not url:
        return False
    try:
        sm = observability_sessionmaker()
        _observability_metadata().create_all(sm.kw["bind"])
        logger.info("관측 DB 스키마 확인 완료 (audit_logs·llm_usage)")
        return True
    except Exception as e:  # noqa: BLE001 — 관측 DB 장애가 앱 기동을 막으면 안 된다
        logger.warning("관측 DB 스키마 준비 실패 (앱은 계속, 기록은 폴백 없이 유실될 수 있음): %s", e)
        return False
