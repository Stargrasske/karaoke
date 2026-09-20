"""Data cleanup, deduplication, and self-healing script for the karaoke platform database."""
from __future__ import annotations

import argparse
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

from karaoke.config import settings
from karaoke.lyrics import clean_title
from karaoke.youtube import fetch_metadata, parse_youtube_title, search
from karaoke.analyze import analyze_audio
from karaoke import localcache, track_analysis

# Two tracks whose durations differ by <= this many seconds are considered the
# "same length" (same song); a larger gap means a different version (edit, live,
# extended, remix) and the tracks are allowed to coexist.
DURATION_TOLERANCE_S = 4.0

# Minimum fuzzy similarity (0..1) on the cleaned title for two tracks to be
# considered the same song title.
TITLE_SIMILARITY = 0.86

# When neither track has a known duration we cannot confirm "same length", so we
# only merge on a near-identical title to stay safe.
TITLE_SIMILARITY_NO_DURATION = 0.97


def _norm_title(title: str) -> str:
    """Normalized, decoration-stripped title for fuzzy comparison."""
    return clean_title(title or "").lower().strip()


def are_titles_similar(t1: str, t2: str, threshold: float = TITLE_SIMILARITY) -> bool:
    """True if two titles are fuzzily the same song (after stripping decorations)."""
    n1, n2 = _norm_title(t1), _norm_title(t2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    return SequenceMatcher(None, n1, n2).ratio() >= threshold


# Words that mark a *different recording* rather than a different spelling.
# A demo, a live take and a remaster are separate performances: they have their
# own lengths, their own timings, and often their own words. Merging them
# attaches one version's synced lyrics to another.
_VERSION_MARKER = re.compile(
    r"\b(demos?|live|unplugged|acoustic|instrumental|a\s?cappella|"
    r"remaster(?:ed)?|remix(?:es)?|rough\s*mix|alternate|alternative\s+take|"
    r"rehearsal|session|radio\s*edit|single\s+version|reprise|"
    r"mono|karaoke|cover|edit)\b",
    re.IGNORECASE,
)


def version_markers(title: str) -> frozenset:
    """Version words present in a title, lowercased.

    Compared as sets rather than tested for presence: two tracks both marked
    "(Live)" are the same live recording under different spellings and should
    still merge. It is a *difference* in markers that means different takes.
    """
    return frozenset(m.group(0).lower().replace(" ", "")
                     for m in _VERSION_MARKER.finditer(title or ""))


def same_version(t1: str, t2: str) -> bool:
    """Whether two titles describe the same recording."""
    return version_markers(t1) == version_markers(t2)


def duration_relation(d1: Optional[float], d2: Optional[float]) -> str:
    """Classify two durations: 'same', 'different', or 'unknown'.

    'same'      -> within DURATION_TOLERANCE_S (same length == same song)
    'different' -> known but far apart (a different version; allowed to coexist)
    'unknown'   -> at least one duration is missing
    """
    if d1 is None or d2 is None:
        return "unknown"
    return "same" if abs(float(d1) - float(d2)) <= DURATION_TOLERANCE_S else "different"


def is_duplicate(t1: dict[str, Any], t2: dict[str, Any]) -> bool:
    """Decide whether two tracks represent the same song and should be merged.

    Rule (per user intent): same/compatible artist AND a matching title, where a
    *different video with the same length* counts as a duplicate, but a
    *different length* is a distinct version and is left alone. When durations
    are unknown we require a near-identical title before merging.
    """
    if not are_artists_compatible(t1["artist"], t2["artist"]):
        return False
    # Checked before the duration relation, and independently of it: a live
    # take can happen to run the same length as the studio cut, so "same
    # length" is not evidence that they are the same recording.
    if not same_version(t1["title"], t2["title"]):
        return False
    rel = duration_relation(t1["duration"], t2["duration"])
    if rel == "same":
        return are_titles_similar(t1["title"], t2["title"])
    if rel == "different":
        return False  # different version — allowed to coexist
    # unknown duration: only merge on a near-exact title
    return are_titles_similar(t1["title"], t2["title"], TITLE_SIMILARITY_NO_DURATION)


def are_artists_compatible(art1: str, art2: str) -> bool:
    """Return True if two artist strings are compatible/highly similar."""
    a1 = art1.lower().strip()
    a2 = art2.lower().strip()
    if not a1 or not a2:
        return False
    if a1 == a2:
        return True
    
    # Check if one is a substring of the other (with length guard)
    if len(a1) >= 3 and a1 in a2:
        return True
    if len(a2) >= 3 and a2 in a1:
        return True

    # Strip common suffixes/decorations
    def clean(s: str) -> str:
        s = s.replace("- topic", "").replace("(topic)", "").replace("official", "").strip()
        # Replace non-alphanumeric punctuation with spaces
        s = re.sub(r'[\(\)\[\]\-\&\,\s]+', ' ', s).strip()
        return s

    c1, c2 = clean(a1), clean(a2)
    if not c1 or not c2:
        return False
    if c1 == c2:
        return True

    # Check if first word matches (common for multi-artist lists)
    w1 = c1.split()[0] if c1.split() else ""
    w2 = c2.split()[0] if c2.split() else ""
    if w1 and w2 and w1 == w2 and len(w1) >= 3:
        return True

    return False


def merge_tracks(track_id_src: int, track_id_dest: int, conn) -> None:
    """Merge Track A (src) into Track B (dest)."""
    cur = conn.cursor()

    # Move sources (deduplicating URLs)
    cur.execute("SELECT url, kind, player_name FROM sources WHERE track_id = %s", (track_id_src,))
    src_sources = cur.fetchall()
    for row in src_sources:
        url, kind, player_name = row["url"], row["kind"], row["player_name"]
        # Check if destination already has this URL
        cur.execute("SELECT source_id FROM sources WHERE track_id = %s AND url = %s", (track_id_dest, url))
        if cur.fetchone():
            # Delete duplicate from source track
            cur.execute("DELETE FROM sources WHERE track_id = %s AND url = %s", (track_id_src, url))
        else:
            # Move source
            cur.execute("UPDATE sources SET track_id = %s WHERE track_id = %s AND url = %s", (track_id_dest, track_id_src, url))

    # Move lyrics (keeping the better ones)
    cur.execute("SELECT lyric_id, kind, source, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = %s", (track_id_src,))
    src_lyrics = cur.fetchall()
    for row in src_lyrics:
        ly_id, kind, source, synced, plain = row["lyric_id"], row["kind"], row["source"], row["synced_lyrics"], row["plain_lyrics"]
        # Check if destination already has lyrics of this kind
        cur.execute("SELECT lyric_id, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = %s AND kind = %s", (track_id_dest, kind))
        dest_ly = cur.fetchone()
        if dest_ly:
            # Determine which is better (synced over plain)
            dest_has_synced = bool(dest_ly["synced_lyrics"])
            src_has_synced = bool(synced)
            if src_has_synced and not dest_has_synced:
                # Replace dest lyrics with src lyrics
                cur.execute("UPDATE lyrics SET source = %s, synced_lyrics = %s, plain_lyrics = %s WHERE lyric_id = %s", 
                            (source, synced, plain, dest_ly["lyric_id"]))
            # Delete the source lyrics row
            cur.execute("DELETE FROM lyrics WHERE lyric_id = %s", (ly_id,))
        else:
            # Move lyrics
            cur.execute("UPDATE lyrics SET track_id = %s WHERE lyric_id = %s", (track_id_dest, ly_id))

    # Move track analysis (keeping the more complete one)
    cur.execute("SELECT * FROM track_analysis WHERE track_id = %s", (track_id_src,))
    src_analysis = cur.fetchone()
    if src_analysis:
        cur.execute("SELECT * FROM track_analysis WHERE track_id = %s", (track_id_dest,))
        dest_analysis = cur.fetchone()
        if dest_analysis:
            # Keep whichever has BPM or more fields populated
            dest_has_bpm = bool(dest_analysis["bpm"])
            src_has_bpm = bool(src_analysis["bpm"])
            if src_has_bpm and not dest_has_bpm:
                # Update dest with src values
                cur.execute(
                    """
                    UPDATE track_analysis 
                    SET detected_key=%s, key_confidence=%s, key_agreement=%s, reference_key=%s, reference_src=%s,
                        resolved_key=%s, key_relation=%s, bpm=%s, method=%s, analyzer_version=%s, updated_at=%s,
                        energy=%s, brightness=%s
                    WHERE track_id=%s
                    """,
                    (src_analysis["detected_key"], src_analysis["key_confidence"], src_analysis["key_agreement"],
                     src_analysis["reference_key"], src_analysis["reference_src"], src_analysis["resolved_key"],
                     src_analysis["key_relation"], src_analysis["bpm"], src_analysis["method"],
                     src_analysis["analyzer_version"], src_analysis["updated_at"], src_analysis["energy"],
                     src_analysis["brightness"], track_id_dest)
                )
            cur.execute("DELETE FROM track_analysis WHERE track_id = %s", (track_id_src,))
        else:
            cur.execute("UPDATE track_analysis SET track_id = %s WHERE track_id = %s", (track_id_dest, track_id_src))

    # Delete the source track row from tracks
    cur.execute("DELETE FROM tracks WHERE track_id = %s", (track_id_src,))


def run_deduplication(conn, dry_run: bool = False) -> int:
    """Scan and merge duplicate tracks representing the same song.

    Two tracks are duplicates when the artist is compatible and the title
    fuzzily matches AND the durations agree (same length). Different lengths are
    treated as distinct versions and left in place (a song may legitimately have
    several videos). Returns the number of merges performed (or would perform in
    dry-run).
    """
    print("=== Step 1: Track Deduplication and Merging ===")
    if dry_run:
        print("(dry-run: no changes will be written)")
    cur = conn.cursor()
    cur.execute("SELECT track_id, artist, title, duration FROM tracks ORDER BY track_id")
    tracks = cur.fetchall()

    # Track which ids have been merged away so we don't reuse them.
    merged_away: set[int] = set()
    merged_count = 0

    for i in range(len(tracks)):
        t1 = tracks[i]
        if t1["track_id"] in merged_away:
            continue
        for j in range(i + 1, len(tracks)):
            t2 = tracks[j]
            if t2["track_id"] in merged_away:
                continue
            if not is_duplicate(t1, t2):
                continue

            # Pick canonical (dest): prefer the one with lyrics, then more
            # sources (more videos), then the longer artist spelling.
            dest, src = _choose_canonical(t1, t2, cur)

            d1, d2 = t1["duration"], t2["duration"]
            dd = "?" if (d1 is None or d2 is None) else f"{abs(d1 - d2):.0f}s"
            print(f"Duplicate (Δdur={dd}): '{src['artist']}' - '{src['title']}' (ID {src['track_id']})")
            print(f"              merge into: '{dest['artist']}' - '{dest['title']}' (ID {dest['track_id']})")

            if not dry_run:
                merge_tracks(src["track_id"], dest["track_id"], conn)
            merged_away.add(src["track_id"])
            merged_count += 1
            if src["track_id"] == t1["track_id"]:
                break  # t1 was merged away; move to next i

    verb = "Would merge" if dry_run else "Merged"
    print(f"Deduplication complete. {verb} {merged_count} duplicate track(s).")
    return merged_count


def _choose_canonical(t1: dict, t2: dict, cur):
    """Return (dest, src): the track to keep and the one to merge away."""
    def score(tid: int) -> tuple[int, int]:
        has_lyrics = bool(cur.execute(
            "SELECT 1 FROM lyrics WHERE track_id = %s AND COALESCE(synced_lyrics,'') || COALESCE(plain_lyrics,'') != ''",
            (tid,)).fetchone())
        nsrc = cur.execute("SELECT count(*) as cnt FROM sources WHERE track_id = %s", (tid,)).fetchone()["cnt"]
        return (1 if has_lyrics else 0, nsrc)

    s1, s2 = score(t1["track_id"]), score(t2["track_id"])
    if s1 != s2:
        return (t1, t2) if s1 > s2 else (t2, t1)
    # Tie-break on the longer (usually fuller) artist spelling.
    return (t1, t2) if len(t1["artist"]) >= len(t2["artist"]) else (t2, t1)


def run_source_healing(conn, limit: int = 50) -> None:
    """Find source URLs for tracks with lyrics that lack any source mapping."""
    print("\n=== Step 2: Auto-filling Source URLs for Orphan Tracks ===")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.track_id, t.artist, t.title FROM tracks t
        WHERE EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id)
          AND NOT EXISTS(SELECT 1 FROM sources s WHERE s.track_id = t.track_id)
        """
    )
    rows = cur.fetchall()
    
    # Filter out obvious garbage/mock entries
    eligible = []
    for r in rows:
        artist, title = r["artist"].strip(), r["title"].strip()
        if not title:
            continue
        if title.lower() in ("analyse", "tui", "test", "demo"):
            # Garbage cleanup: delete them!
            print(f"Pruning mock/test track ID {r['track_id']} ('{artist}' - '{title}')")
            cur.execute("DELETE FROM tracks WHERE track_id = %s", (r["track_id"],))
            cur.execute("DELETE FROM lyrics WHERE track_id = %s", (r["track_id"],))
            cur.execute("DELETE FROM track_analysis WHERE track_id = %s", (r["track_id"],))
            continue
        eligible.append(r)

    print(f"Found {len(eligible)} valid tracks with lyrics but no source URL.")
    
    healed = 0
    for i, r in enumerate(eligible[:limit]):
        artist, title = r["artist"], r["title"]
        query = f"{artist} {title}".strip()
        print(f"[{i+1}/{min(len(eligible), limit)}] Searching YouTube for '{query}'...")
        try:
            results = search(query, limit=1)
            if results:
                url = results[0]["url"]
                # Add to sources
                cur.execute(
                    "INSERT INTO sources (track_id, kind, url, player_name) VALUES (%s, %s, %s, %s)",
                    (r["track_id"], "youtube", url, "browser")
                )
                print(f"  -> Found URL: {url}")
                healed += 1
                time.sleep(1.0)  # Safe throttling
            else:
                print("  -> No YouTube results found.")
        except Exception as e:
            print(f"  -> Search error: {e}")

    print(f"Source URL auto-fill complete. Healed {healed} track(s).")


def run_orphan_cache_healing(conn) -> None:
    """Find downloaded cache files that are missing track/source mappings, heal them, and analyze."""
    print("\n=== Step 3: Self-Healing Orphan Downloaded Cache Files ===")
    yt_dir = Path(settings.youtube_dir)
    if not yt_dir.exists():
        print(f"YouTube cache directory {yt_dir} does not exist.")
        return

    cur = conn.cursor()
    # Get all known YouTube source URLs
    cur.execute("SELECT url FROM sources WHERE kind = 'youtube'")
    known_urls = {row["url"] for row in cur.fetchall() if row["url"]}

    files = list(yt_dir.glob("*.webm"))
    print(f"Found {len(files)} files in YouTube cache.")

    healed_count = 0
    for i, file_path in enumerate(files):
        vid_id = file_path.stem
        url = f"https://www.youtube.com/watch?v={vid_id}"

        if url in known_urls:
            continue

        print(f"Found orphan cache file: {file_path.name}")
        print(f"  -> URL: {url}")
        
        try:
            print("  -> Fetching metadata from YouTube...")
            meta = fetch_metadata(url, download=False)
            title = meta.get("title") or ""
            uploader = meta.get("artist") or "" # uploader is usually returned as artist or can fallback
            duration = meta.get("duration")

            artist_clean, title_clean = parse_youtube_title(title, uploader)
            print(f"  -> Decoded clean metadata: '{artist_clean}' - '{title_clean}'")

            # Check if this track already exists in tracks table
            track_id = find_track_id_by_artist_title(artist_clean, title_clean, conn)
            if not track_id:
                # Create a new track entry and retrieve the auto-generated ID
                cur.execute(
                    "INSERT INTO tracks (artist, title, duration) VALUES (%s, %s, %s) RETURNING track_id",
                    (artist_clean, title_clean, duration)
                )
                row = cur.fetchone()
                track_id = row["track_id"]
                print(f"  -> Created new track record (ID {track_id})")
            else:
                print(f"  -> Reusing existing track record (ID {track_id})")

            # Insert source mapping
            cur.execute(
                "INSERT INTO sources (track_id, kind, url, player_name) VALUES (%s, 'youtube', %s, 'browser')",
                (track_id, url)
            )
            print("  -> Registered source mapping in DB")
            known_urls.add(url)

            # Analyze the local audio file now that it is mapped
            print("  -> Running audio analysis on local file...")
            result = analyze_audio(str(file_path))
            key = result.key
            
            kwargs = {
                "detected_key": key,
                "key_confidence": result.key_confidence,
                "key_agreement": result.key_agreement,
                "bpm": result.bpm,
                "method": result.method,
                "analyzer_version": result.version,
                "conn": conn,
            }
            if hasattr(result, "energy"):
                kwargs["energy"] = getattr(result, "energy", None)
            if hasattr(result, "brightness"):
                kwargs["brightness"] = getattr(result, "brightness", None)
                
            import inspect
            sig = inspect.signature(track_analysis.save_detected)
            for k in list(kwargs.keys()):
                if k not in sig.parameters:
                    del kwargs[k]
                    
            if track_id is not None:
                track_analysis.save_detected(int(track_id), **kwargs)
                print(f"  -> Key/BPM analyzed: key={key.name if key else 'unknown'}, bpm={result.bpm if result.bpm else 'unknown'}")
                healed_count += 1
        except Exception as e:
            print(f"  -> Failed to heal file: {e}")

    print(f"Orphan cache healing complete. Recovered {healed_count} file(s).")


def find_track_id_by_artist_title(artist: str, title: str, conn) -> Optional[int]:
    """Helper to lookup track_id case-insensitively."""
    cur = conn.cursor()
    cur.execute(
        "SELECT track_id FROM tracks WHERE lower(artist) = lower(%s) AND lower(title) = lower(%s)",
        (artist.strip(), title.strip())
    )
    row = cur.fetchone()
    return row["track_id"] if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Karaoke DB cleanup / dedup / self-healing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report duplicates without merging or healing.")
    parser.add_argument("--dedup-only", action="store_true",
                        help="Only run deduplication (skip source/cache healing).")
    args = parser.parse_args()

    print("Using database: PostgreSQL (via localcache.connect())")
    conn = localcache.connect()
    track_analysis.ensure_schema(conn)

    try:
        if args.dry_run:
            # Read-only: report duplicates, touch nothing.
            run_deduplication(conn, dry_run=True)
            return
        # 1. Deduplication
        run_deduplication(conn)
        conn.commit()
        if not args.dedup_only:
            # 2. Source healing
            run_source_healing(conn, limit=30)  # heal up to 30 tracks per run
            conn.commit()
            # 3. Orphan cache healing
            run_orphan_cache_healing(conn)
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
