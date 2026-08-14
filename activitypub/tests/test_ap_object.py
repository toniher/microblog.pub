from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject


def _remote_note(attachment: dict, **extra) -> RemoteObject:
    raw_object = {
        "id": "https://example.com/note/1",
        "type": "Note",
        "attributedTo": LOCAL_ACTOR.ap_id,
        "content": "hello",
        "attachment": [attachment],
        **extra,
    }
    return RemoteObject(raw_object, LOCAL_ACTOR)


def test_video_attachment_parses_blurhash_duration_and_poster() -> None:
    obj = _remote_note(
        {
            "type": "Document",
            "mediaType": "video/mp4",
            "url": "https://example.com/video.mp4",
            "name": "a clip",
            "width": 1280,
            "height": 720,
            "blurhash": "U58E0g",
            "duration": "PT88.654S",
            "icon": {
                "type": "Image",
                "mediaType": "image/webp",
                "url": "https://example.com/poster.webp",
            },
        }
    )

    attachments = obj.attachments
    assert len(attachments) == 1
    attachment = attachments[0]

    assert attachment.blurhash == "U58E0g"
    assert attachment.duration == "PT88.654S"
    assert attachment.duration_seconds == 88.654
    assert attachment.poster_url is not None
    assert "example.com" not in attachment.poster_url  # proxied, not the raw remote URL


def test_video_attachment_icon_as_list() -> None:
    obj = _remote_note(
        {
            "type": "Document",
            "mediaType": "video/mp4",
            "url": "https://example.com/video.mp4",
            "icon": [
                {
                    "type": "Image",
                    "mediaType": "image/webp",
                    "url": "https://example.com/poster.webp",
                }
            ],
        }
    )

    attachment = obj.attachments[0]
    assert attachment.poster_url is not None


def test_attachment_without_icon_has_no_poster_url() -> None:
    obj = _remote_note(
        {
            "type": "Document",
            "mediaType": "image/png",
            "url": "https://example.com/photo.png",
        }
    )

    attachment = obj.attachments[0]
    assert attachment.poster_url is None
    assert attachment.duration is None
    assert attachment.duration_seconds is None


def test_peertube_video_link_gets_poster_from_object_icon() -> None:
    raw_object = {
        "id": "https://example.com/videos/watch/1",
        "type": "Video",
        "attributedTo": LOCAL_ACTOR.ap_id,
        "content": "a peertube video",
        "icon": {
            "type": "Image",
            "url": "https://example.com/preview.jpg",
        },
        "url": [
            {
                "type": "Link",
                "mediaType": "video/mp4",
                "href": "https://example.com/videos/1-1080.mp4",
            }
        ],
    }
    obj = RemoteObject(raw_object, LOCAL_ACTOR)

    attachments = obj.attachments
    assert len(attachments) == 1
    assert attachments[0].poster_url is not None
