import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.utils import setup_auth_access_token
from tests.utils import setup_outbox_note


@pytest.mark.asyncio
async def test_micropub_create(
    client: TestClient,
    async_db_session: AsyncSession,
):
    # test that we create a note via micropub
    await setup_auth_access_token(async_db_session)

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer accesstoken",
    }

    body = {"type": ["h-entry"], "properties": {"content": ["Hello, World!"]}}
    response = client.post("/micropub", json=body, headers=headers)

    # assert success and the location of the new note
    assert response.status_code == 201
    assert "location" in response.headers

    new_path = f"/o/{response.headers.get('Location').split('/')[-1]}"
    res = client.get(new_path)

    # assert the new note is actually at the location
    assert "Hello, World!" in res.text


@pytest.mark.asyncio
async def test_micropub_update(
    client: TestClient,
    async_db_session: AsyncSession,
):
    # create and confirm note to update
    hello_note = setup_outbox_note("123hello123")

    note_path = f"/o/{hello_note.public_id}"
    res = client.get(note_path)

    assert "123hello123" in res.text
    assert "potato" not in res.text

    # get ready to update
    await setup_auth_access_token(async_db_session)

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer accesstoken",
    }

    body = {
        "action": "update",
        "type": ["h-entry"],
        "url": hello_note.ap_id,
        "replace": {"content": "potato"},
    }

    client.post("/micropub", json=body, headers=headers)

    # confirm update
    note_path = f"/o/{hello_note.public_id}"
    response = client.get(note_path)

    assert "123hello123" not in response.text
    assert "potato" in response.text
