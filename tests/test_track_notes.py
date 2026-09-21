"""Notes: text about a track that is not its lyrics.

The case that produced this table: the YouTube Music lyrics panel renders the
artist biography in the same element as the words, so four Wizards of Ooze
tracks had a Dutch Wikipedia paragraph stored as their lyrics -- and when that
was recognised, the paragraph was deleted rather than moved. These tests cover
both halves: that a biography is classified as one, and that classifying it no
longer means losing it.
"""
from __future__ import annotations

import sqlite3

import pytest

from karaoke import localcache, vector_index
from karaoke import ytmusic_lyrics as yl


# The real text, shortened. No provider attribution, long lines, and it names
# its own source -- all three signals that this is not a lyric.
BIOGRAPHY = (
    "De Wizards of Ooze was een Belgische band in de jaren 90, opgericht door "
    "Peter Revalk en Wim Tops. In 1992 brachten ze hun eerste cd uit, 'The "
    "Bone', die goed werd onthaald door de critici.\n"
    "Bron: Wikipedia, 12.483 weergaven, CC-BY"
)

LYRICS = "\n".join([
    "I don't know what I'm doing here",
    "Sitting in the parking lot",
    "Waiting for the sun to clear",
    "You're the only friend I've got",
    "Bron: LyricFind",
])


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "notes.db")
    c.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s)",
              ("Wizards of Ooze", "Bambee!"))
    c.commit()
    yield c
    c.close()


def _track_id(conn) -> int:
    return conn.execute("SELECT track_id FROM tracks").fetchone()["track_id"]


# --- the table -------------------------------------------------------------

def test_a_biography_survives_being_stored(conn):
    tid = _track_id(conn)
    assert localcache.record_note(tid, "biography", BIOGRAPHY,
                                  "ytmusic_panel", conn)
    notes = localcache.notes_for_track(tid, conn)
    assert len(notes) == 1
    assert notes[0]["kind"] == "biography"
    assert "Peter Revalk" in notes[0]["text"]


def test_an_unknown_kind_is_refused_rather_than_filed_as_other(conn):
    """A caller that has not decided what it holds cannot store it."""
    assert not localcache.record_note(_track_id(conn), "misc", "something",
                                      "somewhere", conn)
    assert localcache.notes_for_track(_track_id(conn), conn) == []


def test_re_reading_the_panel_does_not_accumulate_copies(conn):
    """The panel is read on every play; the biography does not change."""
    tid = _track_id(conn)
    for _ in range(5):
        localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    assert len(localcache.notes_for_track(tid, conn)) == 1


def test_a_longer_later_read_wins(conn):
    """A panel read mid-render is truncated, and the full text should replace it."""
    tid = _track_id(conn)
    localcache.record_note(tid, "biography", "De Wizards of Ooze was", "ytmusic_panel", conn)
    localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    stored = localcache.notes_for_track(tid, conn)
    assert len(stored) == 1
    assert "Peter Revalk" in stored[0]["text"]


def test_a_truncated_later_read_does_not_replace_the_full_one(conn):
    tid = _track_id(conn)
    localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    localcache.record_note(tid, "biography", "De Wizards", "ytmusic_panel", conn)
    assert "Peter Revalk" in localcache.notes_for_track(tid, conn)[0]["text"]


def test_different_kinds_coexist_on_one_track(conn):
    """A track can have both a history and a transcription of itself."""
    tid = _track_id(conn)
    localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    localcache.record_note(tid, "transcription", "bambee bambee",
                           "whisper", conn, confidence=0.42)
    kinds = {r["kind"] for r in localcache.notes_for_track(tid, conn)}
    assert kinds == {"biography", "transcription"}


def test_confidence_is_kept_for_a_transcription_and_absent_otherwise(conn):
    """A Whisper guess and a LyricFind biography are not equally trustworthy."""
    tid = _track_id(conn)
    localcache.record_note(tid, "transcription", "words", "whisper", conn,
                           confidence=0.31)
    localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    by_kind = {r["kind"]: r["confidence"]
               for r in localcache.notes_for_track(tid, conn)}
    assert by_kind["transcription"] == pytest.approx(0.31)
    assert by_kind["biography"] is None


def test_empty_text_is_not_stored(conn):
    assert not localcache.record_note(_track_id(conn), "biography", "   ",
                                      "ytmusic_panel", conn)


def test_the_table_is_added_to_a_database_predating_it(tmp_path):
    if getattr(localcache, "_IS_PG", False):
        import pytest
        pytest.skip("Test specifically for SQLite schema migration")
    """Existing DBs must gain the table without a rebuild."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE tracks (track_id INTEGER PRIMARY KEY, "
                "artist TEXT, title TEXT)")
    old.commit()
    old.close()

    c = localcache.connect(path)
    try:
        names = {r["name"] for r in
                 c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "track_notes" in names
    finally:
        c.close()


# --- the classifier --------------------------------------------------------

def _panel(text: str, attribution: str = "") -> yl.PanelLyrics:
    return yl.PanelLyrics(text=text, attribution=attribution)


def test_the_biography_is_recognised_as_a_biography():
    assert yl.classify(_panel(BIOGRAPHY)) == "biography"


def test_real_lyrics_are_still_recognised_as_lyrics():
    assert yl.classify(_panel(LYRICS, "Bron: LyricFind")) == "lyrics"


def test_usable_still_means_lyrics_only():
    """Callers storing into the lyrics table must be unaffected."""
    assert yl.usable(_panel(LYRICS, "Bron: LyricFind"))
    assert not yl.usable(_panel(BIOGRAPHY))


def test_prose_with_a_real_attribution_is_left_unclassified():
    """A spoken-word track is prose but has a provider; do not call it a bio."""
    prose = "\n".join([
        "This is a very long spoken line that runs well past ninety characters "
        "and then keeps going for a good while yet without stopping",
        "And another one exactly like it, equally long, equally unlike anything "
        "that anyone would ever describe as a sung lyric line",
        "A third for good measure which also comfortably exceeds the mean line "
        "length limit that separates prose from lyrics here",
        "And a fourth so that the minimum line count is satisfied too, again "
        "at a length no chorus has ever been written at",
    ])
    assert yl.classify(_panel(prose, "Bron: LyricFind")) is None


def test_short_lyrics_without_an_attribution_are_not_called_a_biography():
    """A panel read too early has no provider yet, but it is not prose."""
    assert yl.classify(_panel(LYRICS)) is None


def test_a_placeholder_panel_classifies_as_nothing():
    assert yl.classify(_panel("Lyrics not available")) is None
    assert yl.classify(None) is None


# --- indexing --------------------------------------------------------------

def test_a_note_becomes_its_own_document(conn):
    """Not a second vector on the track doc: kinds must be separable."""
    tid = _track_id(conn)
    localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    row = next(iter(localcache.iter_note_rows(conn)))

    doc = vector_index.build_note_doc(row, embed=False)
    assert doc["kind"] == "biography"
    assert doc["artist"] == "Wizards of Ooze"
    assert doc["note_source"] == "ytmusic_panel"
    assert "Peter Revalk" in doc["text"]
    assert doc["source"] == "sqlite-note"


def test_note_doc_ids_are_stable_and_distinct_from_lines():
    assert vector_index.note_doc_id(7) == "sqlite-note:7"
    assert vector_index.note_doc_id(7) != vector_index.line_doc_id(7, 0)


def test_the_embedded_text_leads_with_the_artist(conn):
    """A search is far more often "that Belgian nineties band" than a phrase."""
    tid = _track_id(conn)
    localcache.record_note(tid, "biography", BIOGRAPHY, "ytmusic_panel", conn)
    row = next(iter(localcache.iter_note_rows(conn)))
    seen: list[str] = []

    from karaoke import embed as embed_mod

    original = embed_mod.embed_text
    embed_mod.embed_text = lambda text: (seen.append(text), [0.0])[1]
    try:
        vector_index.build_note_doc(row, embed=True)
    finally:
        embed_mod.embed_text = original

    assert seen and seen[0].startswith("Wizards of Ooze Bambee!")


def test_notes_are_not_indexed_unless_asked(conn):
    """Lyric search is the primary use; notes must not dilute it by default."""
    import inspect

    sig = inspect.signature(vector_index.rebuild_from_sqlite)
    assert sig.parameters["include_notes"].default is False
