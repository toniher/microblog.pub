import asyncio
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


def pytest_sessionfinish(session, exitstatus) -> None:
    # aiosqlite connections run a non-daemon worker thread that only stops
    # on close(); without disposing the pool, pytest hangs after the last
    # test instead of exiting.
    asyncio.run(async_engine.dispose())


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
