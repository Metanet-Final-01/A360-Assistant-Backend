"""Alembic 환경 — 앱의 Base.metadata와 DATABASE_URL을 그대로 쓴다.

rag_documents 테이블은 app/rag가 원시 SQL(pgvector 포함)로 관리하므로 Alembic이
건드리지 않도록 제외한다 (autogenerate가 실수로 DROP하지 않게).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# 앱 모델·설정 로딩
from app.db import Base, _database_url
from app import models  # noqa: F401 — Base.metadata에 모든 테이블 등록

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic 관리에서 제외할 테이블 (app/rag 소유)
_EXCLUDED_TABLES = {"rag_documents"}


def _url() -> str:
    """DB URL. configparser(set_main_option)엔 절대 저장하지 않는다.

    configparser의 기본 보간(%-interpolation)이 '%'를 특수문자로 취급해서 비밀번호에
    '%'가 섞이면(RDS 자동생성 비밀번호에서 실제로 발생한 사고) ValueError로 죽는다.
    app.db.run_migrations()가 attributes(순수 dict)에 넣어준 값을 우선 쓰고,
    `alembic` CLI를 직접 실행할 땐(attributes 없음) _database_url()을 바로 쓴다.
    """
    return config.attributes.get("sqlalchemy_url") or _database_url()


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in _EXCLUDED_TABLES:
        return False
    # 제외 테이블에 걸린 인덱스 등도 함께 제외
    if getattr(obj, "table", None) is not None and obj.table.name in _EXCLUDED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
