from datetime import timedelta

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub.tests import factories
from app import models
from app.config import INBOX_RETENTION_DAYS
from app.prune import prune_old_data
from app.utils.datetime import now
from tests.utils import setup_inbox_note
from tests.utils import setup_remote_actor


@pytest.mark.asyncio
async def test_prune_deletes_notifications_for_pruned_inbox_objects(
    db,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """`Notification.inbox_object_id` is a nullable FK with no cascade, and
    SQLite's FK enforcement is off -- so pruning an inbox object without also
    dropping the notifications that reference it leaves them dangling. That
    dangling state is what broke `status_id`/grouped-notification decoding
    for real Mastodon clients.
    """
    ra = setup_remote_actor(respx_mock)
    actor = factories.ActorFactory.from_remote_actor(ra)

    old_note = setup_inbox_note(actor, content="old enough to prune")
    old_note.ap_published_at = now() - timedelta(days=INBOX_RETENTION_DAYS + 1)
    kept_note = setup_inbox_note(actor, content="kept: within retention")
    db.commit()

    async_db_session.add_all(
        [
            models.Notification(
                notification_type=models.NotificationType.MENTION,
                actor_id=actor.id,
                inbox_object_id=old_note.id,
            ),
            models.Notification(
                notification_type=models.NotificationType.MENTION,
                actor_id=actor.id,
                inbox_object_id=kept_note.id,
            ),
        ]
    )
    await async_db_session.commit()

    await prune_old_data(async_db_session)

    remaining_inbox_ids = set(
        (
            await async_db_session.scalars(select(activitypub.models.InboxObject.id))
        ).all()
    )
    assert remaining_inbox_ids == {kept_note.id}

    remaining_notification_targets = set(
        (
            await async_db_session.scalars(select(models.Notification.inbox_object_id))
        ).all()
    )
    assert remaining_notification_targets == {kept_note.id}
