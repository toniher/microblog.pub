import email.utils
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from dateutil.parser import isoparse


def parse_isoformat(isodate: str) -> datetime:
    return isoparse(isodate).astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


def parse_retry_after(retry_after: str) -> datetime | None:
    try:
        # Retry-After: 120
        seconds = int(retry_after)
    except ValueError:
        # Retry-After: Wed, 21 Oct 2015 07:28:00 GMT
        dt_tuple = email.utils.parsedate_tz(retry_after)
        if dt_tuple is None:
            return None

        seconds = int(email.utils.mktime_tz(dt_tuple) - time.time())

    return now() + timedelta(seconds=seconds)
