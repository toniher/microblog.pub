from typing import Any
from typing import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import DB_PATH
from app.config import DEBUG
from app.config import SQLALCHEMY_DATABASE_URL

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 15}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
async_engine = create_async_engine(
    DATABASE_URL, future=True, echo=DEBUG, connect_args={"timeout": 15}
)
async_session = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine, "connect")
@event.listens_for(async_engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    # WAL is already persisted in the DB file by the 6286262bb466 migration;
    # reasserting it here keeps freshly created DBs (including the in-memory
    # test DB) consistent with production. synchronous=NORMAL is safe under
    # WAL (a crash can't corrupt, only lose the last transactions) and avoids
    # a full fsync on every write. temp_store=MEMORY keeps ORDER BY spills
    # off disk; cache_size/mmap_size raise the working set SQLite keeps hot.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.close()


Base: Any = declarative_base()
metadata_obj = MetaData()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
