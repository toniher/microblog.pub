import os
from typing import Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from activitypub.tests.factories import _Session
from app.database import Base
from app.database import async_engine
from app.database import async_session
from app.database import engine
from app.main import app

os.environ["MICROBLOGPUB_CONFIG_FILE"] = "tests.toml"


@pytest.fixture(autouse=True)
def _reset_scoped_session() -> Generator:
    # `_Session` is a module-level scoped_session shared by every factory
    # across the whole test run. Several tests (e.g. the async
    # process_next_outgoing_activity ones) write through it without using the
    # `db` fixture, so nothing closes it and its identity map keeps the
    # committed rows. Meanwhile every test drops and recreates the schema,
    # resetting autoincrement PKs — so the next insert reuses id=1 and
    # collides with the stale (now expired/GC'd) state left in the map,
    # which surfaces as the flaky "NoneType has no __dict__" / "transaction
    # is closed" errors in CI. Reset the session around every test.
    _Session.remove()
    yield
    _Session.remove()


@pytest_asyncio.fixture
async def async_db_session():
    async with async_session() as session:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield session
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def db() -> Generator:
    Base.metadata.create_all(bind=engine)
    with _Session() as db_session:
        try:
            yield db_session
        finally:
            db_session.close()
            Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db) -> Generator:
    with TestClient(app) as c:
        yield c
