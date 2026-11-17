import base64
from datetime import datetime
from typing import Any

from dateutil.parser import isoparse
from sqlalchemy import ColumnElement
from sqlalchemy import tuple_


def encode_cursor(val: datetime) -> str:
    return base64.urlsafe_b64encode(val.isoformat().encode()).decode()


def decode_cursor(cursor: str) -> datetime:
    return isoparse(base64.urlsafe_b64decode(cursor).decode())


def encode_keyset_cursor(ts: datetime, row_id: int) -> str:
    """A cursor for `keyset_before`: the timestamp alone can't mark a page
    boundary, since many rows share one (remote `published` values have
    second precision)."""
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{row_id}".encode()).decode()


def keyset_before(ts_col: Any, id_col: Any, cursor: str) -> ColumnElement[bool]:
    """The rows after the cursor in `ORDER BY ts_col DESC, id_col DESC` order.

    A row-value comparison: SQLite still serves it from an index ending in
    `ts_col` (`id` being the rowid every index ends with). A timestamp-only
    cursor from `encode_cursor` (links minted before keyset cursors) falls
    back to the old `ts_col < ts`.
    """
    raw = base64.urlsafe_b64decode(cursor).decode()
    ts, sep, row_id = raw.rpartition("|")
    if not sep:
        return ts_col < isoparse(raw)
    return tuple_(ts_col, id_col) < tuple_(isoparse(ts), int(row_id))
