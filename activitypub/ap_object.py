import enum
import hashlib
import mimetypes
import re
from datetime import datetime
from functools import cached_property
from typing import Any

import pydantic
from bs4 import BeautifulSoup  # type: ignore
from mistletoe import markdown  # type: ignore

from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import Actor
from activitypub.actor import RemoteActor

# TODO: What can we refactor in the library from these imports and config?
from app.config import ID
from app.media import proxied_media_url
from app.utils.datetime import now
from app.utils.datetime import parse_isoformat


# TODO implement supported ap_types as an ENUMERATOR
# to avoid all the "random" post_type/object_type strings around the code!
class ObjectType(enum.Enum):
    ANNOUNCE = "Announce"
    ARTICLE = "Article"
    CREATE = "Create"
    FOLLOW = "Follow"
    LIKE = "Like"
    NOTE = "Note"
    UNDO = "Undo"
    UPDATE = "Update"


class Object:
    @property
    def is_from_db(self) -> bool:
        return False

    @property
    def is_from_outbox(self) -> bool:
        return False

    @property
    def is_from_inbox(self) -> bool:
        return False

    @cached_property
    def ap_type(self) -> str:  # TODO: Covert to ObjectType
        return ap.as_list(self.ap_object["type"])[0]

    @property
    def ap_object(self) -> ap.RawObject:
        raise NotImplementedError

    @property
    def ap_id(self) -> str:
        return ap.get_id(self.ap_object["id"])

    @property
    def ap_actor_id(self) -> str:
        return ap.get_actor_id(self.ap_object)

    @cached_property
    def ap_published_at(self) -> datetime | None:
        # TODO: default to None? or now()?
        if "published" in self.ap_object:
            return parse_isoformat(self.ap_object["published"])
        elif "created" in self.ap_object:
            return parse_isoformat(self.ap_object["created"])
        return None

    @property
    def actor(self) -> Actor:
        raise NotImplementedError()

    @cached_property
    def visibility(self) -> ap.VisibilityEnum:
        return ap.object_visibility(self.ap_object, self.actor)

    @property
    def ap_context(self) -> str | None:
        return self.ap_object.get("context") or self.ap_object.get("conversation")

    @property
    def sensitive(self) -> bool:
        # Some servers send an explicit `"sensitive": null` (or another
        # non-bool); `dict.get(k, False)` only substitutes the default when the
        # key is *absent*, so coerce to guarantee the declared bool return.
        # A null here serializes to `"sensitive": null` in the Mastodon API,
        # which breaks strict clients (Tusky/Fedilab) that expect a non-null
        # boolean and silently drop the whole timeline page.
        return bool(self.ap_object.get("sensitive"))

    @property
    def tags(self) -> list[ap.RawObject]:
        return ap.as_list(self.ap_object.get("tag", []))

    @cached_property
    def inlined_images(self) -> set[str]:
        image_urls: set[str] = set()
        if not self.content:
            return image_urls

        soup = BeautifulSoup(self.content, "html5lib")
        imgs = soup.find_all("img")

        for img in imgs:
            if not img.attrs.get("src"):
                continue

            image_urls.add(img.attrs["src"])

        return image_urls

    @cached_property
    def attachments(self) -> list["Attachment"]:
        attachments = []
        for obj in ap.as_list(self.ap_object.get("attachment", [])):
            if obj.get("type") == "PropertyValue":
                continue

            if obj.get("type") == "Link":
                attachments.append(
                    Attachment.model_validate(
                        {
                            "proxiedUrl": None,
                            "resizedUrl": None,
                            "mediaType": None,
                            "type": "Link",
                            "url": obj["href"],
                        }
                    )
                )
                continue

            proxied_url = proxied_media_url(obj["url"])
            attachments.append(
                Attachment.model_validate(
                    {
                        "proxiedUrl": proxied_url,
                        "resizedUrl": (
                            proxied_url + "/740"
                            if obj.get("mediaType", "").startswith("image")
                            else None
                        ),
                        "posterUrl": _extract_poster_url(obj),
                        **obj,
                    }
                )
            )

        # Also add any video Link (for PeerTube compat)
        if self.ap_type == "Video":
            video_poster_url = _extract_poster_url(self.ap_object)
            for link in ap.as_list(self.ap_object.get("url", [])):
                if (isinstance(link, dict)) and link.get("type") == "Link":
                    if link.get("mediaType", "").startswith("video"):
                        proxied_url = proxied_media_url(link["href"])
                        attachments.append(
                            Attachment(
                                type="Video",
                                mediaType=link["mediaType"],
                                url=link["href"],
                                proxiedUrl=proxied_url,
                                posterUrl=video_poster_url,
                            )
                        )
                        break
                    elif link.get("mediaType", "") == "application/x-mpegURL":
                        for tag in ap.as_list(link.get("tag", [])):
                            if tag.get("mediaType", "").startswith("video"):
                                proxied_url = proxied_media_url(tag["href"])
                                attachments.append(
                                    Attachment(
                                        type="Video",
                                        mediaType=tag["mediaType"],
                                        url=tag["href"],
                                        proxiedUrl=proxied_url,
                                        posterUrl=video_poster_url,
                                    )
                                )
                                break
        return attachments

    @cached_property
    def url(self) -> str | None:
        obj_url = self.ap_object.get("url")
        if isinstance(obj_url, str) and obj_url:
            return obj_url
        elif obj_url:
            for u in ap.as_list(obj_url):
                if isinstance(u, str) and u:
                    return u
                if not isinstance(u, dict):
                    continue
                if u.get("type") == "Link" and u.get("href"):
                    return u["href"]
                if u.get("mediaType") == "text/html" and u.get("href"):
                    return u["href"]

        return self.ap_id

    @cached_property
    def content(self) -> str | None:
        content = self.ap_object.get("content")
        if not content:
            return None

        # PeerTube returns the content as markdown
        if self.ap_object.get("mediaType") == "text/markdown":
            content = markdown(content)

        return content

    @property
    def summary(self) -> str | None:
        return self.ap_object.get("summary")

    @property
    def name(self) -> str | None:
        return self.ap_object.get("name")

    @cached_property
    def permalink_id(self) -> str:
        return (
            "permalink-"
            + hashlib.md5(
                self.ap_id.encode(),
                usedforsecurity=False,
            ).hexdigest()
        )

    @property
    def activity_object_ap_ids(self) -> list[str]:
        """Every object an activity addresses.

        `Flag` is the one activity that addresses several at once: Mastodon
        reports the actor plus each reported status in a single `object` array.
        Everything else is single-valued, and yields a one-element list.
        """
        if "object" not in self.ap_object:
            return []

        return [ap.get_id(obj) for obj in ap.as_list(self.ap_object["object"])]

    @property
    def activity_object_ap_id(self) -> str | None:
        ap_ids = self.activity_object_ap_ids
        if ap_ids:
            return ap_ids[0]

        return None

    @property
    def in_reply_to(self) -> str | None:
        raw_in_reply_to = self.ap_object.get("inReplyTo")
        if not raw_in_reply_to:
            return None

        for item in ap.as_list(raw_in_reply_to):
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict):
                if item_id := item.get("id"):
                    return ap.get_id(item_id)
                if href := item.get("href"):
                    return href

        return None

    @property
    def is_local_reply(self) -> bool:
        if not self.in_reply_to:
            return False

        return bool(
            self.in_reply_to.startswith(ID) and self.content  # Hide votes from Question
        )

    @property
    def is_in_reply_to_from_inbox(self) -> bool | None:
        if not self.in_reply_to:
            return None

        return not self.in_reply_to.startswith(LOCAL_ACTOR.ap_id)

    @property
    def has_ld_signature(self) -> bool:
        return bool(self.ap_object.get("signature"))

    @property
    def is_poll_ended(self) -> bool:
        if self.poll_end_time:
            return now() > self.poll_end_time
        return False

    @cached_property
    def poll_items(self) -> list[ap.RawObject] | None:
        return self.ap_object.get("oneOf") or self.ap_object.get("anyOf")

    @cached_property
    def poll_end_time(self) -> datetime | None:
        # Some polls may not have an end time
        if self.ap_object.get("endTime"):
            return parse_isoformat(self.ap_object["endTime"])

        return None

    @cached_property
    def poll_voters_count(self) -> int | None:
        if not self.poll_items:
            return None
        # Only Mastodon set this attribute
        if self.ap_object.get("votersCount"):
            return self.ap_object["votersCount"]
        else:
            voters_count = 0
            for item in self.poll_items:
                voters_count += item.get("replies", {}).get("totalItems", 0)

            return voters_count

    @cached_property
    def is_one_of_poll(self) -> bool:
        return bool(self.ap_object.get("oneOf"))


def _to_camel(string: str) -> str:
    cased = "".join(word.capitalize() for word in string.split("_"))
    return cased[0:1].lower() + cased[1:]


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(alias_generator=_to_camel)


_XSD_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?$"
)


def _parse_xsd_duration(value: str) -> float | None:
    """Parse the subset of xsd:duration used for media (PT[nH][nM][n.nS])."""
    match = _XSD_DURATION_RE.match(value)
    if not match:
        return None
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def format_xsd_duration(seconds: float) -> str:
    """The inverse of _parse_xsd_duration, e.g. 88.654 -> "PT88.654S"."""
    return f"PT{seconds:.3f}S"


def _extract_poster_url(obj: dict[str, Any]) -> str | None:
    """Proxy the attachment's (or the top-level object's, for PeerTube)
    `icon`/`image` as a poster URL — either may be a single dict or a list.
    """
    for key in ("icon", "image"):
        candidate = obj.get(key)
        if not candidate:
            continue
        for item in ap.as_list(candidate):
            if isinstance(item, str):
                return proxied_media_url(item)
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return proxied_media_url(item["url"])
    return None


class Attachment(BaseModel):
    type: str
    media_type: str | None = None
    name: str | None = None
    url: str

    # Extra fields for the templates (and only for media)
    proxied_url: str | None = None
    resized_url: str | None = None

    width: int | None = None
    height: int | None = None

    blurhash: str | None = None
    duration: str | None = None  # xsd:duration, e.g. "PT88.654S"
    poster_url: str | None = None  # proxied AP `icon`/`image` url (video only)

    # Only populated for local attachments (from Upload.has_audio) — remote
    # AP payloads don't carry an equivalent field, so it stays None ("cannot
    # tell") rather than being guessed at.
    has_audio: bool | None = None

    # Cropping hint, `[x, y]` each in [-1, 1]. Not part of core ActivityStreams;
    # federated (both ways) as the Pleroma-style `focalPoint` extension.
    focal_point: list[float] | None = None

    @property
    def mimetype(self) -> str:
        mimetype = self.media_type
        if not mimetype:
            mimetype, _ = mimetypes.guess_type(self.url)

        if not mimetype:
            return "unknown"

        return mimetype.split("/")[-1]

    @property
    def duration_seconds(self) -> float | None:
        if not self.duration:
            return None
        return _parse_xsd_duration(self.duration)

    @property
    def focus(self) -> tuple[float, float] | None:
        if not self.focal_point or len(self.focal_point) != 2:
            return None
        return (self.focal_point[0], self.focal_point[1])


class RemoteObject(Object):
    def __init__(self, raw_object: ap.RawObject, actor: Actor):
        self._raw_object = raw_object
        self._actor = actor

        if self._actor.ap_id != ap.get_actor_id(self._raw_object):
            raise ValueError(f"Invalid actor {self._actor.ap_id}")

    @classmethod
    async def from_raw_object(
        cls,
        raw_object: ap.RawObject,
        actor: Actor | None = None,
    ):
        # Pre-fetch the actor
        actor_id = ap.get_actor_id(raw_object)
        if actor_id == LOCAL_ACTOR.ap_id:
            _actor = LOCAL_ACTOR
        elif actor:
            if actor.ap_id != actor_id:
                raise ValueError(
                    f"Invalid actor, got {actor.ap_id}, " f"expected {actor_id}"
                )
            _actor = actor  # type: ignore
        else:
            _actor = RemoteActor(
                ap_actor=await ap.fetch(ap.get_actor_id(raw_object)),
            )

        return cls(raw_object, _actor)

    @property
    def og_meta(self) -> list[dict[str, Any]] | None:
        return None

    @property
    def ap_object(self) -> ap.RawObject:
        return self._raw_object

    @property
    def actor(self) -> Actor:
        return self._actor
