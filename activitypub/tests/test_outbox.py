import pytest
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub.models import OutboxObject


@pytest.mark.asyncio
async def test_fetch_outbox__empty(async_db_session: AsyncSession) -> None :
    result = await boxes.fetch_outbox(async_db_session)
    assert len(result) == 0

@pytest.mark.asyncio
async def test_fetch_outbox__note(async_db_session: AsyncSession) -> None:
    test_note = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "THIS IS A TEST",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC
    )
    result = await boxes.fetch_outbox(async_db_session)
    assert len(result) == 1
    assert isinstance(result[0], OutboxObject)
    assert result[0].ap_type == ObjectType.NOTE.value

