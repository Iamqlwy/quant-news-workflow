"""PostgreSQL async engine + session factory (wfuse)"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import settings

if not settings.database_url:
    raise RuntimeError(
        "database_url is empty. Set DATABASE_URL in .env or environment, "
        "e.g. postgresql+asyncpg://user:pass@localhost:5432/dbname"
    )

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_pre_ping=True,
    pool_recycle=1800,  # 30分钟后回收连接，防止 PostgreSQL 服务端断开导致的 ConnectionDoesNotExistError
    connect_args={"server_settings": {"timezone": "Asia/Shanghai"}},
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
