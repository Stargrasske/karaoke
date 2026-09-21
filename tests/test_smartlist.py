"""Building a playlist from tracks that feel alike.

Matching is on the *shape* of the sentiment rather than the dominant label: two
songs can both read "sad" while one is mournful and the other half furious, and
a playlist that files them together is not much of a playlist.
"""
import pytest

from karaoke import smartlist


def _cand(track_id, artist, title, vector, bpm=None, hits=10, dominant="sad"):
    return smartlist.Candidate(track_id=track_id, artist=artist, title=title,
                               vector=vector, hits=hits, dominant=dominant,
                               bpm=bpm)


# -- comparing feelings -------------------------------------------------

def test_identical_feelings_match_completely():
    v = (0.5, 0.3, 0.1, 0.1)
    assert smartlist.cosine(v, v) == pytest.approx(1.0)


def test_opposite_feelings_do_not_match():
    happy = (1.0, 0.0, 0.0, 0.0)
    sad = (0.0, 1.0, 0.0, 0.0)
    assert smartlist.cosine(happy, sad) == pytest.approx(0.0)


def test_intensity_does_not_change_the_match():
    """A quietly sad lyric and a relentlessly sad one point the same way."""
    faint = (0.1, 0.2, 0.0, 0.1)
    strong = (0.5, 1.0, 0.0, 0.5)
    assert smartlist.cosine(faint, strong) == pytest.approx(1.0)


def test_the_shape_separates_two_sad_songs():
    """Both dominant-sad, but one is half angry: they should not be twins."""
    mournful = (0.0, 0.9, 0.0, 0.1)
    furious = (0.0, 0.5, 0.5, 0.0)
    assert smartlist.cosine(mournful, furious) < 0.85


def test_an_empty_feeling_matches_nothing():
    assert smartlist.cosine((0.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)) == 0.0


def test_mismatched_vectors_are_refused():
    with pytest.raises(ValueError, match="dimension mismatch"):
        smartlist.cosine((1.0, 0.0), (1.0, 0.0, 0.0, 0.0))


# -- tempo --------------------------------------------------------------

def test_matching_tempos_score_full():
    assert smartlist.tempo_affinity(120.0, 120.0) == pytest.approx(1.0)


def test_distant_tempos_score_zero():
    assert smartlist.tempo_affinity(80.0, 200.0) == 0.0


def test_an_unknown_tempo_is_reported_as_unknown():
    assert smartlist.tempo_affinity(None, 120.0) is None
    assert smartlist.tempo_affinity(120.0, 0) is None


def test_an_unknown_tempo_neither_rewards_nor_punishes():
    """The bug this replaced: dropping the term left unanalysed tracks
    with an untouched sentiment score while analysed ones were blended
    downward, so tracks floated to the top for having less known about them."""
    seed = _cand(1, "S", "Seed", (0.0, 1.0, 0.0, 0.0), bpm=120.0)
    same_words = (0.0, 1.0, 0.0, 0.0)
    close = smartlist.score(seed, _cand(2, "A", "Close", same_words, bpm=120.0))
    unknown = smartlist.score(seed, _cand(3, "B", "Unknown", same_words, bpm=None))
    clash = smartlist.score(seed, _cand(4, "C", "Clash", same_words, bpm=220.0))
    assert close.score > unknown.score > clash.score


def test_tempo_can_be_switched_off():
    seed = _cand(1, "S", "Seed", (0.0, 1.0, 0.0, 0.0), bpm=120.0)
    other = _cand(2, "A", "Other", (0.0, 1.0, 0.0, 0.0), bpm=220.0)
    assert smartlist.score(seed, other, tempo_weight=0).score == pytest.approx(1.0)


# -- assembling the list ------------------------------------------------

def test_the_seed_is_not_in_its_own_playlist():
    seed = _cand(1, "A", "Seed", (0.0, 1.0, 0.0, 0.0))
    out = smartlist.similar_to(seed, [seed], limit=5)
    assert out == []


def test_results_are_ordered_by_score():
    seed = _cand(1, "S", "Seed", (0.0, 1.0, 0.0, 0.0))
    pool = [
        _cand(2, "A", "Close", (0.0, 1.0, 0.0, 0.0)),
        _cand(3, "B", "Middling", (0.0, 0.7, 0.7, 0.0)),
        _cand(4, "C", "Far", (1.0, 0.0, 0.0, 0.0)),
    ]
    scores = [m.score for m in smartlist.similar_to(seed, pool, limit=5)]
    assert scores == sorted(scores, reverse=True)


def test_one_artist_cannot_take_over_the_playlist():
    """Seeded on an album, every track on it scores alike -- correct, and
    useless as a playlist."""
    seed = _cand(1, "S", "Seed", (0.0, 1.0, 0.0, 0.0))
    pool = [_cand(i + 10, "Samey Band", f"Track {i}", (0.0, 1.0, 0.0, 0.0))
            for i in range(6)]
    pool.append(_cand(99, "Someone Else", "One", (0.0, 0.95, 0.05, 0.0)))
    out = smartlist.similar_to(seed, pool, limit=5, per_artist=2)
    artists = [m.candidate.artist for m in out]
    assert artists.count("Samey Band") == 2
    assert "Someone Else" in artists


def test_the_artist_cap_can_be_lifted():
    seed = _cand(1, "S", "Seed", (0.0, 1.0, 0.0, 0.0))
    pool = [_cand(i + 10, "Band", f"T{i}", (0.0, 1.0, 0.0, 0.0)) for i in range(5)]
    assert len(smartlist.similar_to(seed, pool, limit=5, per_artist=0)) == 5


def test_the_limit_is_respected():
    seed = _cand(1, "S", "Seed", (0.0, 1.0, 0.0, 0.0))
    pool = [_cand(i + 10, f"Band{i}", f"T{i}", (0.0, 1.0, 0.0, 0.0))
            for i in range(20)]
    assert len(smartlist.similar_to(seed, pool, limit=7)) == 7


# -- seeds --------------------------------------------------------------

def test_a_mood_seed_points_at_one_feeling():
    seed = smartlist.mood_seed("angry")
    assert seed.vector == (0.0, 0.0, 1.0, 0.0)


def test_a_seed_track_is_found_loosely():
    """Editions and a leading "The" must not stop a seed being found."""
    pool = [_cand(1, "The Slits", "Newtown (2020 Remaster)", (0.0, 1.0, 0.0, 0.0))]
    assert smartlist.find_seed(pool, "Slits", "Newtown") is not None


def test_a_missing_seed_is_none():
    pool = [_cand(1, "A", "B", (0.0, 1.0, 0.0, 0.0))]
    assert smartlist.find_seed(pool, "Nobody", "Nothing") is None


# -- the pool -----------------------------------------------------------

def test_sparse_lyrics_are_kept_out_of_the_pool(tmp_path):
    """One mood word gives a vector that matches everything in its direction."""
    from karaoke import localcache

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.execute("""
            INSERT INTO tracks (track_id, artist, title) VALUES
                (1, 'A', 'Rich'), (2, 'B', 'Sparse');
            INSERT INTO lyrics (track_id, kind, plain_lyrics) VALUES
                (1, 'approved', 'sad sad lonely cry alone tears hurt broken grief'),
                (2, 'approved', 'la la la la la la la la la la la la la sad');
        """)
        conn.commit()
        pool = smartlist.load_candidates(conn)
        assert [c.title for c in pool] == ["Rich"]
    finally:
        conn.close()
