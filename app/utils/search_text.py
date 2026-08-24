"""Derives the normalized, indexable text search reads and writes.

See `PLAN-search.md` for why this exists: SQLite's `LIKE`/`lower()` only fold
ASCII case, so normalizing here at write time -- rather than folding at query
time -- is both what makes `josé` match `JOSÉ` and what lets the FTS5 trigram
index in `activitypub/models.py` serve the query (see `glob_pattern`'s
docstring for the constraint that keeps that index live).
"""

import unicodedata
from typing import Any

from bs4 import BeautifulSoup  # type: ignore


def normalize(s: str) -> str:
    """Unicode-correct case folding: NFC first so combining-mark variants of
    the same character compare equal, then `casefold()`, which (unlike
    `lower()`) folds beyond ASCII -- `İ`/`i̇`, `ẞ`/`ss`, etc."""
    return unicodedata.normalize("NFC", s).casefold()


# The FTS5 trigram tokenizer only serves a `GLOB` query when the pattern's
# metacharacters (`*`, `?`, `[`) are escaped as single-character classes
# (`[*]`, `[?]`, `[[]`) -- there is no `ESCAPE` clause for `GLOB`, and adding
# one to `LIKE` is what silently drops that query back to a full scan. `%`
# and `_` are ordinary characters to `GLOB`, so they need no escaping here.
_GLOB_METACHARS = {"*": "[*]", "?": "[?]", "[": "[[]"}


def glob_pattern(query: str) -> str:
    """`query` as a `GLOB` substring pattern, with its own metacharacters
    escaped so they match literally."""
    escaped = "".join(_GLOB_METACHARS.get(ch, ch) for ch in query)
    return f"*{escaped}*"


def object_search_text(ap_object: dict[str, Any]) -> str:
    """Indexed text for a status: its visible content plus every link
    target, so a URL search still works without the raw markup making a
    stray `<p>` match every row."""
    content = ap_object.get("content") or ""
    soup = BeautifulSoup(content, "html5lib")
    parts = [soup.get_text(" ")]
    parts.extend(a["href"] for a in soup.find_all("a", href=True))
    return normalize(" ".join(parts))


def actor_search_text(ap_actor: dict[str, Any]) -> str:
    """Indexed text for an actor: handle, display name, and AP ID."""
    parts = [
        ap_actor.get("preferredUsername") or "",
        ap_actor.get("name") or "",
        ap_actor.get("id") or "",
    ]
    return normalize(" ".join(p for p in parts if p))
