from app.utils import diff


def _segments(old: str, new: str) -> list[diff.DiffSegment]:
    return diff.word_diff(old, new).segments


def test_tokenize__roundtrips() -> None:
    text = "hello   world\n\nnew paragraph\twith a tab"
    assert "".join(diff.tokenize(text)) == text


def test_word_diff__identical() -> None:
    segments = _segments("hello world", "hello world")
    assert segments == [diff.DiffSegment("equal", "hello world")]
    assert not diff.has_changes(segments)


def test_word_diff__replace_emits_delete_then_insert() -> None:
    segments = _segments("hello wrold", "hello world")
    ops = [s.op for s in segments]
    assert ops.index("delete") < ops.index("insert")


def test_word_diff__roundtrip_invariants() -> None:
    old = "Hello wrold, this is a note.\n\nRemoved sentence entirely."
    new = "Hello world, this is a note about ActivityPub.\n\nA brand new sentence."
    segments = _segments(old, new)
    assert "".join(s.text for s in segments if s.op != "insert") == old
    assert "".join(s.text for s in segments if s.op != "delete") == new


def test_word_diff__preserves_newlines() -> None:
    old = "one paragraph"
    new = "one paragraph\n\nanother paragraph"
    segments = _segments(old, new)
    inserted = "".join(s.text for s in segments if s.op == "insert")
    assert "\n\n" in inserted


def test_word_diff__empty_old() -> None:
    segments = _segments("", "hello world")
    assert segments == [diff.DiffSegment("insert", "hello world")]
    assert diff.has_changes(segments)


def test_word_diff__empty_new() -> None:
    segments = _segments("hello world", "")
    assert segments == [diff.DiffSegment("delete", "hello world")]
    assert diff.has_changes(segments)


def test_word_diff__adjacent_same_op_segments_are_merged() -> None:
    segments = _segments("a b c", "a x c")
    ops = [s.op for s in segments]
    for i in range(len(ops) - 1):
        assert ops[i] != ops[i + 1]


def test_word_diff__long_repetitive_input_still_diffs() -> None:
    words = ["foo"] * 250
    old = " ".join(words)
    words[125] = "bar"
    new = " ".join(words)
    segments = _segments(old, new)
    assert diff.has_changes(segments)
    deletes = [s for s in segments if s.op == "delete"]
    inserts = [s for s in segments if s.op == "insert"]
    assert len(deletes) == 1
    assert len(inserts) == 1
    assert "bar" in inserts[0].text


def test_word_diff__small_input_is_precise() -> None:
    result = diff.word_diff("hello wrold", "hello world")
    assert result.is_coarse is False


def test_word_diff__oversized_input_falls_back_to_coarse() -> None:
    """A precise diff is quadratic; past the limit we accept a coarser one
    rather than burning tens of seconds of CPU on a single request."""
    old = "word " * (diff._PRECISE_TOKEN_LIMIT + 10)
    new = old.replace("word", "changed", 1)

    result = diff.word_diff(old, new)

    assert result.is_coarse is True
    assert diff.has_changes(result.segments)


def test_word_diff__oversized_input_is_fast() -> None:
    """Regression guard for the quadratic blowup: without the size limit this
    input takes tens of seconds."""
    import time

    old = " ".join(f"w{i}" for i in range(20000))
    new = old.replace("w10000", "CHANGED", 1)

    start = time.perf_counter()
    result = diff.word_diff(old, new)
    elapsed = time.perf_counter() - start

    assert result.is_coarse is True
    assert elapsed < 2.0, f"diff took {elapsed:.1f}s"


def test_revision_text__prefers_source() -> None:
    result = diff.revision_text({"content": "<p>ignored</p>"}, "raw markdown")
    assert result == diff.RevisionText("raw markdown", False)


def test_revision_text__falls_back_to_html() -> None:
    result = diff.revision_text({"content": "<p>hello world</p>"}, None)
    assert result.is_approximate is True
    assert "hello" in result.text
    assert "world" in result.text


def test_revision_text__no_source_no_content() -> None:
    result = diff.revision_text({}, None)
    assert result == diff.RevisionText("", True)


def test_revision_text__html_fallback_is_not_wrapped() -> None:
    long_paragraph = "word " * 60
    result = diff.revision_text({"content": f"<p>{long_paragraph.strip()}</p>"}, None)
    assert "\n" not in result.text.strip()
