"""Tests for LRC parsing and the LRCLIB client (network mocked)."""
from karaoke.lyrics import parse_lrc, fetch_lrclib, clean_title, Lyrics


def test_parse_lrc_basic():
    lrc = "[00:36.70] first line\n[00:43.51] second line"
    lines = parse_lrc(lrc)
    assert lines == [(36.70, "first line"), (43.51, "second line")]


def test_parse_lrc_multiple_timestamps_per_line():
    lrc = "[00:10.00][01:10.00] chorus"
    lines = parse_lrc(lrc)
    assert lines == [(10.0, "chorus"), (70.0, "chorus")]


def test_parse_lrc_sorts_by_time():
    lrc = "[01:00.00] later\n[00:30.00] earlier"
    lines = parse_lrc(lrc)
    assert [t for t, _ in lines] == [30.0, 60.0]


def test_parse_lrc_skips_metadata_and_empty():
    lrc = "[ar: Someone]\n[00:05.00]\n[00:06.00] real line"
    lines = parse_lrc(lrc)
    assert lines == [(6.0, "real line")]


def test_parse_lrc_millisecond_normalization():
    # two-digit fraction is centiseconds -> .5s ; three-digit is ms
    assert parse_lrc("[00:01.5] a") == [(1.5, "a")]
    assert parse_lrc("[00:01.500] a") == [(1.5, "a")]


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Returns queued responses in order, ignoring URL/params."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


def test_fetch_lrclib_get_hit():
    payload = {
        "syncedLyrics": "[00:01.00] hi",
        "plainLyrics": "hi",
    }
    sess = _FakeSession([_FakeResp(200, payload)])
    ly = fetch_lrclib("A", "B", session=sess)
    assert isinstance(ly, Lyrics)
    assert ly.source == "lrclib"
    assert ly.has_synced
    assert ly.lines == [(1.0, "hi")]
    assert sess.calls[0][0].endswith("/api/get")


def test_fetch_lrclib_falls_back_to_search():
    # /api/get 404, then /api/search returns a synced hit
    sess = _FakeSession([
        _FakeResp(404, {}),
        _FakeResp(200, [
            {"syncedLyrics": "", "plainLyrics": "plain only"},
            {"syncedLyrics": "[00:02.00] synced", "plainLyrics": "x"},
        ]),
    ])
    ly = fetch_lrclib("A", "B", session=sess)
    assert ly.has_synced
    assert ly.lines == [(2.0, "synced")]
    assert sess.calls[1][0].endswith("/api/search")


def test_fetch_lrclib_total_miss():
    sess = _FakeSession([_FakeResp(404, {}), _FakeResp(200, [])])
    ly = fetch_lrclib("A", "B", session=sess)
    assert ly.source == "none"
    assert not ly.has_synced


def test_clean_title_strips_dash_suffixes():
    assert clean_title("The Wind is Whispering - Live") == "The Wind is Whispering"
    assert clean_title("Song - Radio Edit") == "Song"
    assert clean_title("Track - Remastered 2011") == "Track"
    assert clean_title("Tune - 2009 Remaster") == "Tune"


def test_clean_title_strips_paren_suffixes():
    assert clean_title("Song (Remastered)") == "Song"
    assert clean_title("Song (Live)") == "Song"
    assert clean_title("Song (feat. Someone)") == "Song"
    assert clean_title("Song (Radio Edit)") == "Song"


def test_clean_title_strips_stacked_suffixes():
    assert clean_title("Song - Live (Remastered 2011)") == "Song"


def test_clean_title_leaves_clean_titles_untouched():
    assert clean_title("No One Knows") == "No One Knows"
    assert clean_title("A-Punk") == "A-Punk"  # hyphen inside a word, no suffix kw
    assert clean_title("") == ""


def test_fetch_lrclib_retries_with_cleaned_title():
    # Exact "X - Live" misses (get 404 + empty search); cleaned "X" then hits.
    sess = _FakeSession([
        _FakeResp(404, {}),                       # get, exact title
        _FakeResp(200, []),                       # search, exact title -> empty
        _FakeResp(200, {"syncedLyrics": "[00:03.00] hey", "plainLyrics": "hey"}),  # get, cleaned
    ])
    ly = fetch_lrclib("Ween", "The Wind is Whispering - Live", session=sess)
    assert ly.has_synced
    assert ly.lines == [(3.0, "hey")]
    # Third call used the cleaned title.
    assert sess.calls[2][1]["track_name"] == "The Wind is Whispering"


def test_fetch_lrclib_no_retry_when_title_clean():
    # A clean title that misses should NOT trigger a second (identical) round.
    sess = _FakeSession([_FakeResp(404, {}), _FakeResp(200, [])])
    ly = fetch_lrclib("A", "No One Knows", session=sess)
    assert ly.source == "none"
    assert len(sess.calls) == 2


# --- channel names arriving as the artist ----------------------------------
#
# A track learned from a browser tab keeps whatever the uploader called their
# channel. "Queen Official" then fails to match the same song stored as
# "Queen", so the library ends up holding both.

def test_clean_artist_strips_a_channel_suffix():
    from karaoke.lyrics import clean_artist

    assert clean_artist("Queen Official") == "Queen"
    assert clean_artist("ModjoOfficial") == "Modjo"
    assert clean_artist("Gnome Official") == "Gnome"


def test_clean_artist_strips_vevo():
    from karaoke.lyrics import clean_artist

    assert clean_artist("QueenVEVO") == "Queen"


def test_clean_artist_still_strips_topic():
    from karaoke.lyrics import clean_artist

    assert clean_artist("Portishead - Topic") == "Portishead"


def test_clean_artist_leaves_real_names_alone():
    """Music and Records are not stripped: plenty of acts genuinely carry them,
    and removing those would rename the artist rather than clean it."""
    from karaoke.lyrics import clean_artist

    for name in ("Massive Attack", "Lesfm - Music for Study and Relaxing",
                 "Def Leppard", "Sub Pop Records"):
        assert clean_artist(name) == name


def test_clean_artist_never_empties_a_name():
    """An act actually called Official keeps its name rather than vanishing."""
    from karaoke.lyrics import clean_artist

    assert clean_artist("Official") == "Official"
    assert clean_artist("VEVO") == "VEVO"


def test_fetch_lrclib_falls_back_to_search_when_get_is_plain_only():
    """If /api/get returns HTTP 200 with only plain lyrics, it must still search for synced."""
    sess = _FakeSession([
        _FakeResp(200, {"plainLyrics": "Hello world", "syncedLyrics": None}),  # get returns plain
        _FakeResp(200, [                                                        # search finds synced
            {"trackName": "Song", "plainLyrics": "Hello world", "syncedLyrics": "[00:01.00] Hello world"}
        ]),
    ])
    ly = fetch_lrclib("Artist", "Song", session=sess)
    assert ly.has_synced
    assert ly.lines == [(1.0, "Hello world")]
    assert len(sess.calls) == 2
    assert "/api/get" in sess.calls[0][0]
    assert "/api/search" in sess.calls[1][0]


def test_fetch_lrclib_tries_cleaned_artist_and_page_title():
    """Tries cleaned artist (- Topic) and page title ((Official Video)) if exact misses."""
    sess = _FakeSession([
        _FakeResp(404, {}),  # get: Kavinsky - Topic, Nightcall (Official Video)
        _FakeResp(200, []),  # search: Kavinsky - Topic, Nightcall (Official Video)
        _FakeResp(404, {}),  # get: Kavinsky - Topic, Nightcall
        _FakeResp(200, []),  # search: Kavinsky - Topic, Nightcall
        _FakeResp(404, {}),  # get: Kavinsky, Nightcall (Official Video)
        _FakeResp(200, []),  # search: Kavinsky, Nightcall (Official Video)
        _FakeResp(200, {"syncedLyrics": "[00:05.00] Nightcall", "plainLyrics": "Nightcall"}),  # get: Kavinsky, Nightcall
    ])
    ly = fetch_lrclib("Kavinsky - Topic", "Nightcall (Official Video)", session=sess)
    assert ly.has_synced
    assert ly.lines == [(5.0, "Nightcall")]
    assert sess.calls[-1][1]["artist_name"] == "Kavinsky"
    assert sess.calls[-1][1]["track_name"] == "Nightcall"
