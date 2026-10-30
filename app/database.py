from typing import Any
from typing import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import sessionmaker

from app.config import DB_PATH
from app.config import DEBUG
from app.config import SQLALCHEMY_DATABASE_URL

# SQLAlchemy 2.x pools file DBs (QueuePool) where 1.4 opened one connection
# per session (NullPool), and deprecates its implicit pick for in-memory DBs.
# Pin the 1.4 choices: the PRAGMA note below relies on NullPool, and the
# in-memory test DB must stay a single shared connection.
_IS_MEMORY_DB = "mode=memory" in str(DB_PATH) or str(DB_PATH) == ":memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
    poolclass=pool.SingletonThreadPool if _IS_MEMORY_DB else pool.NullPool,
)
SessionLocal = sessionmaker(autoflush=False, bind=engine)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
async_engine = create_async_engine(
    DATABASE_URL,
    echo=DEBUG,
    connect_args={"timeout": 15},
    poolclass=pool.StaticPool if _IS_MEMORY_DB else pool.NullPool,
)
async_session = async_sessionmaker(async_engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
@event.listens_for(async_engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    # WAL is already persisted in the DB file by the 6286262bb466 migration;
    # reasserting it here keeps freshly created DBs (including the in-memory
    # test DB) consistent with production. synchronous=NORMAL is safe under
    # WAL (a crash can't corrupt, only lose the last transactions) and avoids
    # a full fsync on every write. mmap_size keeps the hot working set in the
    # shared, reclaimable OS page cache. cache_size and temp_store stay at
    # SQLite's defaults on purpose: both are private (anonymous) memory per
    # connection, there is one connection per session (NullPool) in each of the
    # four processes, so a large cache is thrown away on close yet still
    # inflates RSS, and in-memory sorts over a GB-sized DB spill into RAM.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.close()


class Base(DeclarativeBase):
    pass


metadata_obj = MetaData()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
