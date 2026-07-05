# Per-worker SQLite isolation + SQLite pragma configuration for parallel tests.
#
# Engines/sessions for app.database are replaced at module level *before*
# tests/factories.py is first imported, so all factory classes automatically
# pick up the per-worker session.

import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import scoped_session
from sqlalchemy.orm import sessionmaker

os.environ["MICROBLOGPUB_CONFIG_FILE"] = "tests.toml"


# ── 1. Determine a unique per-worker DB path ─────────────────────────────────


def _worker_id() -> str:
    """Unique identifier for this test worker (xdist gw0/gw1/… or PID)."""
    return os.getenv("PYTEST_XDIST_WORKER") or f"pid{os.getpid()}"


def _worker_db_path() -> Path:
    """Return an isolated SQLite file path for this worker."""
    tmp = Path(tempfile.gettempdir()) / "microblogpub-tests"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp / f"test-{_worker_id()}.sqlite3"


# ── 2. SQLite pragma hook ─────────────────────────────────────────────────────


def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    """Apply WAL journal mode and other pragmas on every new connection."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")  # 10000 ms (10 s) – prevents lock errors in CI
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()


# ── 3. Build per-worker engines ───────────────────────────────────────────────

_DB_PATH = _worker_db_path()
_SYNC_URL = f"sqlite:///{_DB_PATH}"
_ASYNC_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

_test_engine = create_engine(
    _SYNC_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
)
event.listen(_test_engine, "connect", _set_sqlite_pragmas)

_test_async_engine = create_async_engine(
    _ASYNC_URL,
    future=True,
    echo=False,
    connect_args={"timeout": 15},
)
event.listen(_test_async_engine.sync_engine, "connect", _set_sqlite_pragmas)

_TestSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=_test_engine
)
_test_async_session = sessionmaker(
    _test_async_engine, class_=AsyncSession, expire_on_commit=False
)


# ── 4. Patch app.database *before* importing anything that depends on it ──────

import app.database as _app_db  # noqa: E402

_app_db.engine = _test_engine
_app_db.SessionLocal = _TestSessionLocal
_app_db.async_engine = _test_async_engine
_app_db.async_session = _test_async_session


# ── 5. Import factory module (picks up patched SessionLocal) ─────────────────

import activitypub.tests.factories as _factories  # noqa: E402

# Refresh factory session to ensure it uses the per-worker engine, then update
# each SQLAlchemy factory's internal session reference.
_factories._Session = scoped_session(_TestSessionLocal)
for _f in (
    _factories.ActorFactory,
    _factories.OutboxObjectFactory,
    _factories.OutgoingActivityFactory,
    _factories.InboxObjectFactory,
    _factories.FollowerFactory,
    _factories.FollowingFactory,
):
    _f._meta.sqlalchemy_session = _factories._Session


# ── 6. Remaining top-level imports ───────────────────────────────────────────

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base  # noqa: E402
from app.main import app  # noqa: E402


# ── 7. Session-scoped cleanup: dispose engines and remove worker DB file ──────


@pytest.fixture(scope="session", autouse=True)
def _cleanup_worker_db():
    yield
    _test_engine.dispose()
    _test_async_engine.sync_engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_DB_PATH) + suffix)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# ── 8. Per-test DB fixtures ───────────────────────────────────────────────────


@pytest_asyncio.fixture
async def async_db_session():
    # Create schema before opening the session to avoid DDL/DML contention.
    async with _test_async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with _test_async_session() as session:
        yield session
    # Session is fully closed here; safe to run DDL.
    async with _test_async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def db() -> Generator:
    Base.metadata.create_all(bind=_test_engine)
    with _factories._Session() as db_session:
        try:
            yield db_session
        finally:
            db_session.close()
            _factories._Session.remove()
            Base.metadata.drop_all(bind=_test_engine)


@pytest.fixture
def client(db) -> Generator:
    with TestClient(app) as c:
        yield c
