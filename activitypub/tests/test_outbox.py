import pytest
from activitypub import boxes
from sqlalchemy.ext.asyncio import AsyncSession

@pytest.mark.asyncio
async def test_fetch_outbox__empty(async_db_session: AsyncSession) -> None :
    result = await boxes.fetch_outbox(async_db_session)
    assert len(result) == 0

# TODO: Finish testing fetch_outbox
# 1 - is the list populated with OutboxObjects???
