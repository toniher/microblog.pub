from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from activitypub import activitypub as ap
from activitypub.tests import factories
from app import config
from app import models


def test_webmention__resolves_target_by_alias(db: Session, client: TestClient) -> None:
    public_id = "note-with-alias"
    note = factories.OutboxObjectFactory(
        public_id=public_id,
        ap_type="Note",
        ap_id=f"http://localhost:8000/o/{public_id}",
        ap_object={"type": "Note", "content": "hello"},
        visibility=ap.VisibilityEnum.PUBLIC,
        alias="my-post",
    )
    target = f"http://localhost:8000/{config.ALIAS_URL_PREFIX}/{note.alias}"
    source = "https://example.com/reply"

    with mock.patch(
        "app.webmentions.microformats.fetch_and_parse",
        return_value=({}, f'<a href="{target}">mentioned</a>'),
    ):
        response = client.post(
            "/webmentions",
            data={"source": source, "target": target},
        )

    assert response.status_code == 200

    webmention = db.query(models.Webmention).one()
    assert webmention.outbox_object_id == note.id
    assert webmention.source == source
    assert webmention.target == target
