"""
SYSTEM DESIGN CONCEPT: Connection Pooling

At 2,300 reads/sec peak, opening a new DB connection per request would be catastrophic.
Each PostgreSQL connection uses ~5MB RAM and takes ~50ms to establish.
Connection pooling maintains a warm pool of connections reused across requests.

AsyncPG + SQLAlchemy async engine:
  pool_size=20:     20 persistent connections always open
  max_overflow=10:  burst up to 30 total connections under load
  pool_timeout=30:  fail fast if no connection available (don't queue forever)
"""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    pool_pre_ping=True,  # verify connection alive before use (handles DB restarts)
    echo=(settings.app_env == "development"),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency: yields a DB session, auto-closes after request."""
    async with AsyncSessionLocal() as session:
        yield session
