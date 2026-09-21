"""Build a Spotify playlist from the karaoke-ready tracks in SQLite.

"Known songs" means tracks that carry approved *synced* lyrics — those are the
ones the player can actually run a karaoke session on, so they are what belongs
in the playlist.

Each track needs a Spotify URI. Stored ``sources`` rows of kind ``spotify``
already carry one; everything else is resolved through the Spotify search API,
which is where the misses come from (YouTube-only finds, obscure releases).

Run:  ``karaoke-spotify-playlist --dry-run``   to see what would be added
      ``karaoke-spotify-playlist``             to create/update the playlist
"""
from __future__ import annotations

import argparse
import re
import psycopg
from psycopg import Connection, Cursor
from dataclasses import dataclass, field
from typing import Optional

from . import localcache
from .logger import log

DEFAULT_PLAYLIST_NAME = "Karaoke (synced lyrics)"
DEFAULT_DESCRIPTION = "Tracks with time-synced lyrics in the local karaoke library."

_SPOTIFY_TRACK_ID = re.compile(
    r"(?:spotify:track:|open\.spotify\.com/track/)([A-Za-z0-9]+)"
)


def primary_artist(artist: str) -> str:
    """Return just the lead artist from a credited list.

    Library rows carry the full credit ("Motorpsycho, Bent Sæther, Hans Magnus
    Ryan") where Spotify indexes the primary artist only, so the filtered query
    finds nothing until the credit is trimmed.
    """
    lead = re.split(r"\s*(?:,|&| feat\.?| ft\.?| x | with )\s*", artist.strip(),
                    maxsplit=1, flags=re.IGNORECASE)[0]
    return lead.strip() or artist.strip()


def search_title(title: str) -> str:
    """Return a title trimmed of the decoration Spotify does not index."""
    from .lyrics import clean_page_title, clean_title

    t = clean_title(clean_page_title(title))
    # "Instant Crush ft. Julian Casablancas" -> "Instant Crush"
    t = re.split(r"\s*(?:\(|\[)?\s*(?:feat\.?|ft\.?)\s+", t, maxsplit=1,
                 flags=re.IGNORECASE)[0]
    return t.strip(" -–—([") or title.strip()


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens of length > 2, for loose comparison."""
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").casefold()) if len(w) > 2}


def track_matches(artist: str, title: str,
                  result_artists: list[str], result_title: str) -> bool:
    """Check a loose search result really is this artist's song.

    Free-text search happily returns a different artist's song of the same
    name, so a relaxed query is only trusted when both the title and the artist
    share real words with the result.
    """
    want_title, got_title = _tokens(title), _tokens(result_title)
    if not want_title or not (want_title & got_title):
        return False
    want_artist = _tokens(artist)
    if not want_artist:
        return True
    got_artist = _tokens(" ".join(result_artists))
    return bool(want_artist & got_artist)


@dataclass
class Candidate:
    """One karaoke-ready track and the Spotify URI resolved for it."""

    artist: str
    title: str
    uri: str = ""
    resolved_by: str = ""   # stored | search | unresolved


@dataclass
class PlaylistResult:
    """Outcome of a playlist build."""

    playlist_id: str = ""
    name: str = ""
    candidates: int = 0
    resolved: int = 0
    added: int = 0
    already_present: int = 0
    unresolved: list[Candidate] = field(default_factory=list)
    dry_run: bool = False
    completed: bool = True   # False when a rate limit cut resolution short


def extract_track_uri(url: str) -> str:
    """Return a ``spotify:track:<id>`` URI from a stored source URL, or ""."""
    m = _SPOTIFY_TRACK_ID.search(url or "")
    return f"spotify:track:{m.group(1)}" if m else ""


def karaoke_tracks(conn: Connection) -> list[Candidate]:
    """Return tracks that have approved synced lyrics, with any stored URI.

    A left join keeps tracks whose only source is YouTube; those fall through
    to the search step rather than being dropped.
    """
    rows = conn.execute(
        """
        SELECT t.artist, t.title,
               (SELECT s.url FROM sources s
                 WHERE s.track_id = t.track_id AND s.kind = 'spotify'
                 LIMIT 1) AS spotify_url
        FROM tracks t
        JOIN lyrics l ON l.track_id = t.track_id
        WHERE l.kind = 'approved'
          AND length(COALESCE(l.synced_lyrics, '')) > 0
          AND length(TRIM(COALESCE(t.artist, ''))) > 0
          AND length(TRIM(COALESCE(t.title, ''))) > 0
                ORDER BY lower(t.artist), lower(t.title)
        """
    ).fetchall()

    out: list[Candidate] = []
    for r in rows:
        uri = extract_track_uri(r["spotify_url"] or "")
        out.append(Candidate(
            artist=r["artist"], title=r["title"],
            uri=uri, resolved_by="stored" if uri else "",
        ))
    return out


def resolve_uris(
    candidates: list[Candidate],
    client,
    *,
    conn: Optional[Connection] = None,
) -> bool:
    """Fill in missing URIs via Spotify search, in place.

    Every hit is written back as a ``spotify`` source so later runs read it from
    SQLite instead of spending search quota again — the searches are by far the
    most expensive part of a build, and they are the same every time.

    Returns True if the pass completed, False if it stopped early on a rate
    limit. Stopping matters: continuing would mark every remaining track
    "unresolved" when the truth is simply that we stopped being allowed to ask.
    """
    from .spotify_client import SpotifyRateLimited

    for c in candidates:
        if c.uri:
            continue
        try:
            uri = client.search_track(c.artist, c.title)
        except SpotifyRateLimited as exc:
            log.warning("spotify: %s — stopping resolution early", exc)
            print(f"  ! {exc}")
            print("  ! Remaining tracks left unresolved; re-run later to finish.")
            return False
        except Exception as exc:
            log.debug("spotify search failed for %s - %s: %s", c.artist, c.title, exc)
            uri = None
        if uri:
            c.uri, c.resolved_by = uri, "search"
            if conn is not None:
                _remember_uri(c, conn)
        else:
            c.resolved_by = "unresolved"
    return True


def _remember_uri(c: Candidate, conn: Connection) -> None:
    """Persist a searched-out URI as a spotify source, best-effort."""
    try:
        track_id = c.uri.rsplit(":", 1)[-1]
        localcache.add_track_source(
            c.artist, c.title,
            url=f"https://open.spotify.com/track/{track_id}",
            kind="spotify", conn=conn,
        )
    except Exception as exc:
        log.debug("could not cache spotify uri for %s - %s: %s", c.artist, c.title, exc)


def build_playlist(
    *,
    name: str = DEFAULT_PLAYLIST_NAME,
    description: str = DEFAULT_DESCRIPTION,
    public: bool = False,
    dry_run: bool = False,
    client=None,
    conn: Optional[Connection] = None,
) -> PlaylistResult:
    """Create or update the karaoke playlist and return what happened.

    Re-running is safe: an existing playlist of the same name is reused and only
    URIs it does not already contain are appended.
    """
    own = conn is None
    c = conn or localcache.connect()
    try:
        candidates = karaoke_tracks(c)

        result = PlaylistResult(name=name, candidates=len(candidates), dry_run=dry_run)
        if not candidates:
            return result

        if client is None:
            from .spotify_client import SpotifyClient
            client = SpotifyClient()

        result.completed = resolve_uris(candidates, client, conn=c)
    finally:
        if own:
            c.close()

    result.unresolved = [x for x in candidates if not x.uri]
    # Preserve order while dropping duplicate URIs (same song, two track rows).
    seen: set[str] = set()
    uris = [c.uri for c in candidates
            if c.uri and not (c.uri in seen or seen.add(c.uri))]
    result.resolved = len(uris)

    if dry_run:
        return result

    playlist_id = client.find_playlist(name)
    if playlist_id:
        existing = set(client.playlist_track_uris(playlist_id))
        new_uris = [u for u in uris if u not in existing]
        result.already_present = len(uris) - len(new_uris)
    else:
        playlist_id = client.create_playlist(name, description=description,
                                             public=public)
        new_uris = uris

    result.playlist_id = playlist_id
    if new_uris:
        result.added = client.add_playlist_tracks(playlist_id, new_uris)
    return result


def playlist_main(argv: Optional[list[str]] = None) -> int:
    """Run the ``karaoke-spotify-playlist`` CLI."""
    ap = argparse.ArgumentParser(
        prog="karaoke-spotify-playlist",
        description="Build a Spotify playlist from tracks with synced lyrics",
    )
    ap.add_argument("--name", default=DEFAULT_PLAYLIST_NAME, help="playlist name")
    ap.add_argument("--public", action="store_true",
                    help="make the playlist public (default: private)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and report, but do not touch Spotify")
    ap.add_argument("--show-unresolved", action="store_true",
                    help="list tracks with no Spotify match")
    args = ap.parse_args(argv)

    res = build_playlist(name=args.name, public=args.public, dry_run=args.dry_run)

    print(f"Karaoke tracks with synced lyrics : {res.candidates}")
    print(f"Resolved to a Spotify URI         : {res.resolved}")
    print(f"No Spotify match                  : {len(res.unresolved)}")
    if not res.completed:
        print("  (stopped early on a Spotify rate limit — counts are incomplete)")
    if args.dry_run:
        print("\n(dry run — nothing was created or modified)")
    else:
        print(f"Playlist                          : {res.name} ({res.playlist_id})")
        print(f"Added this run                    : {res.added}")
        print(f"Already present                   : {res.already_present}")

    if args.show_unresolved and res.unresolved:
        print("\nNo Spotify match:")
        for c in res.unresolved:
            print(f"  {c.artist} - {c.title}")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual run
    raise SystemExit(playlist_main())
