"""Alembic 환경 — 앱의 Base.metadata와 DATABASE_URL을 그대로 쓴다.

rag_documents 테이블은 app/rag가 원시 SQL(pgvector 포함)로 관리하므로 Alembic이
건드리지 않도록 제외한다 (autogenerate가 실수로 DROP하지 않게).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 앱 모델·설정 로딩
from app.db import Base, _database_url
from app import models  # noqa: F401 — Base.metadata에 모든 테이블 등록

config = context.config
config.set_main_option("sqlalchemy.url", _database_url())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic 관리에서 제외할 테이블 (app/rag 소유)
_EXCLUDED_TABLES = {"rag_documents"}


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in _EXCLUDED_TABLES:
        return False
    # 제외 테이블에 걸린 인덱스 등도 함께 제외
    if getattr(obj, "table", None) is not None and obj.table.name in _EXCLUDED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
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
