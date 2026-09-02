import re
from difflib import SequenceMatcher
from typing import Any
from typing import Literal
from typing import NamedTuple

import html2text

_TOKEN_RE = re.compile(r"\s+|\S+")

# Distinct from app.templates.H2T, which sets ignore_links=True and keeps the
# default body_width=78: hard-wrapping would inject bogus line-break diffs and
# dropped links would hide URL edits.
_H2T = html2text.HTML2Text()
_H2T.body_width = 0
_H2T.ignore_links = False
_H2T.ignore_images = False

DiffOp = Literal["equal", "delete", "insert"]

# SequenceMatcher's matching is quadratic in the token count, so a precise
# (autojunk=False) diff of a long post is ruinously slow: measured on this
# machine, ~4k tokens takes 0.5s, ~20k takes 13s and ~40k takes 57s. Above this
# limit we let difflib's autojunk heuristic drop over-common tokens, which is
# ~1500x faster (tens of milliseconds even at 40k tokens) at the cost of a
# coarser diff -- a big changed block instead of the exact words. Callers
# surface that tradeoff via DiffResult.is_coarse.
_PRECISE_TOKEN_LIMIT = 4000


class DiffSegment(NamedTuple):
    op: DiffOp
    text: str


class DiffResult(NamedTuple):
    segments: list[DiffSegment]
    is_coarse: bool


class RevisionText(NamedTuple):
    text: str
    is_approximate: bool


def tokenize(text: str) -> list[str]:
    """Split into words *and* whitespace runs, so "".join(tokenize(t)) == t.

    Keeping whitespace as its own tokens means newlines survive the diff and
    paragraph structure is preserved in the rendered output.
    """
    return _TOKEN_RE.findall(text)


def revision_text(ap_object: dict[str, Any], source: str | None) -> RevisionText:
    if source:
        return RevisionText(source, False)
    content = ap_object.get("content")
    if content:
        return RevisionText(_H2T.handle(content).strip(), True)
    return RevisionText("", True)


def _append(segments: list[DiffSegment], op: DiffOp, tokens: list[str]) -> None:
    if not tokens:
        return
    text = "".join(tokens)
    if segments and segments[-1].op == op:
        segments[-1] = DiffSegment(op, segments[-1].text + text)
    else:
        segments.append(DiffSegment(op, text))


def word_diff(old: str, new: str) -> DiffResult:
    """Word-level diff of two texts. Pure CPU work, and superlinear in the
    input size -- call it via `asyncio.to_thread`, never inline in a handler."""
    old_tokens = tokenize(old)
    new_tokens = tokenize(new)
    # autojunk=True treats any token appearing in >1% of a 200+ element
    # sequence as junk, which degrades the diff (every " " whitespace token
    # qualifies), so keep it off for the normal case and only accept that
    # degradation for inputs too big to diff precisely in reasonable time.
    is_coarse = max(len(old_tokens), len(new_tokens)) > _PRECISE_TOKEN_LIMIT
    matcher = SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=is_coarse)
    segments: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            _append(segments, "equal", old_tokens[i1:i2])
        elif tag == "delete":
            _append(segments, "delete", old_tokens[i1:i2])
        elif tag == "insert":
            _append(segments, "insert", new_tokens[j1:j2])
        else:  # "replace" -> removal immediately followed by insertion
            _append(segments, "delete", old_tokens[i1:i2])
            _append(segments, "insert", new_tokens[j1:j2])
    return DiffResult(segments, is_coarse)


def has_changes(segments: list[DiffSegment]) -> bool:
    return any(s.op != "equal" for s in segments)
