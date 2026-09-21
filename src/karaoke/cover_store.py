"""Keep cover art after its URL stops working.

Art is fetched for display and then thrown away, which is fine right up until
the source disappears — Spotify's image URLs expire and YouTube thumbnails
change with re-uploads, so the art for a track fetched today may simply not be
retrievable next month. The rendering is cheap to keep and impossible to
recreate once the source is gone.

**What is stored is the sampled grid, not the rendered text.** The terminal
rendering is background-coloured cells (:func:`karaoke.coverart.to_text`), and
its size is fixed at render time. Keeping the grid instead means one stored
copy serves any panel smaller than it, while keeping finished text would pin
the art to whatever width the window happened to be. Measured at 64x32 a grid
is about 3.7 KB compressed, so the whole library is a few megabytes.

**And it lives in SQLite, not OpenSearch.** :mod:`karaoke.vector_index` is
explicit that SQLite is the source of truth and the index holds *rebuildable*
documents. Art whose source URL has expired cannot be rebuilt — that is the
entire point of keeping it — so the index may mirror it but must never be the
only copy, or a rebuild would quietly destroy what it cannot fetch again.

Art is keyed by its own identity rather than by track, because an album cover
is shared by every track on the record and storing it once per track would
multiply it by the track count for no gain.
"""
from __future__ import annotations

import hashlib
import psycopg
from psycopg import Connection, Cursor
import time
import zlib
from pathlib import Path
from typing import Any, Optional

from .logger import log

Grid = list[list[tuple[int, int, int]]]

# Stored resolution. Large enough to serve any panel the TUI actually uses,
# small enough that the whole library is a few megabytes: 64x32 is 6 KB raw and
# about 3.7 KB compressed. Downscaling from this is fine; upscaling is not, so
# the number is chosen to be comfortably above real panel sizes rather than to
# match one.
STORE_COLS = 64
STORE_ROWS = 32


def art_key(source_url: str, pixels: Optional[Grid] = None) -> str:
    """Stable identity for one piece of art.

    Keyed on the *pixels* when they are available, so the same cover reached
    through two different URLs — a Spotify CDN link and a YouTube thumbnail of
    the same record — is stored once. Falls back to the URL when it is not.
    """
    if pixels:
        raw = _pack_raw(pixels)
        return "px:" + hashlib.sha256(raw).hexdigest()[:32]
    return "url:" + hashlib.sha256((source_url or "").encode()).hexdigest()[:32]


def _pack_raw(pixels: Grid) -> bytes:
    return bytes(v for row in pixels for cell in row for v in cell)


def pack(pixels: Grid) -> bytes:
    """Compress a grid for storage."""
    return zlib.compress(_pack_raw(pixels), 9)


def unpack(blob: bytes, cols: int, rows: int) -> Optional[Grid]:
    """Restore a grid, or None if the stored bytes do not match the shape."""
    try:
        raw = zlib.decompress(blob)
    except zlib.error:
        log.debug("stored art could not be decompressed")
        return None
    if len(raw) < cols * rows * 3:
        return None
    out: Grid = []
    for y in range(rows):
        line = []
        for x in range(cols):
            i = (y * cols + x) * 3
            line.append((raw[i], raw[i + 1], raw[i + 2]))
        out.append(line)
    return out


def rescale(pixels: Grid, cols: int, rows: int) -> Optional[Grid]:
    """Nearest-neighbour resize, downward only.

    Refuses to enlarge: upscaling a 64-cell grid to 96 invents detail and looks
    worse than re-sampling the original, which the caller can still do while
    the source exists.
    """
    if not pixels or cols < 1 or rows < 1:
        return None
    have_rows, have_cols = len(pixels), len(pixels[0])
    if cols > have_cols or rows > have_rows:
        return None
    if cols == have_cols and rows == have_rows:
        return pixels
    out: Grid = []
    for y in range(rows):
        sy = min(have_rows - 1, y * have_rows // rows)
        out.append([pixels[sy][min(have_cols - 1, x * have_cols // cols)]
                    for x in range(cols)])
    return out


def ensure_table(conn: Connection) -> None:
    """Create the art tables in databases predating them."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cover_art (
            art_key    TEXT PRIMARY KEY,
            cols       INTEGER NOT NULL,
            rows       INTEGER NOT NULL,
            pixels     BLOB NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            stored_at  REAL NOT NULL
        );
        -- Separate from cover_art so one cover serves a whole album rather
        -- than being stored once per track.
        CREATE TABLE IF NOT EXISTS track_art (
            track_id  INTEGER PRIMARY KEY,
            art_key   TEXT NOT NULL,
            noted_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_track_art_key ON track_art (art_key);
    """)
    conn.commit()


def store(pixels: Grid, conn: Connection, *,
          source_url: str = "") -> Optional[str]:
    """Keep one grid, returning its key."""
    if not pixels or not pixels[0]:
        return None
    key = art_key(source_url, pixels)
    conn.execute(
        """
        INSERT INTO cover_art (art_key, cols, rows, pixels, source_url, stored_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(art_key) DO UPDATE SET
            source_url = CASE WHEN excluded.source_url != ''
                              THEN excluded.source_url ELSE cover_art.source_url END
        """,
        (key, len(pixels[0]), len(pixels), pack(pixels), source_url, time.time()),
    )
    conn.commit()
    return key


def link(track_id: int, key: str, conn: Connection) -> None:
    """Point a track at a stored cover."""
    conn.execute(
        "INSERT INTO track_art (track_id, art_key, noted_at) VALUES (%s, %s, %s)"
        " ON CONFLICT(track_id) DO UPDATE SET art_key = excluded.art_key,"
        " noted_at = excluded.noted_at",
        (track_id, key, time.time()))
    conn.commit()


def capture(track_id: int, source: Path, conn: Connection, *,
            source_url: str = "") -> Optional[str]:
    """Sample a local image or video frame and keep it for this track."""
    from . import coverart

    # Fitted, not forced into a fixed box. coverart.render measures the source
    # so "a 16:9 frame and a square cover both keep their proportions", and
    # sampling everything at STORE_COLS x STORE_ROWS threw that away: a
    # 1280x720 thumbnail belongs at 64x14 and was being stored at 64x32,
    # stretched vertically by more than double. The table keeps cols and rows
    # per row, so a variable shape costs nothing.
    size = coverart.probe_size(source)
    fitted = coverart.fit(size, STORE_COLS, STORE_ROWS) if size else None
    cols, rows = fitted if fitted else (STORE_COLS, STORE_ROWS)
    pixels = coverart.sample(source, cols, rows)
    if not pixels:
        log.debug("could not sample art from %s", source)
        return None
    key = store(pixels, conn, source_url=source_url)
    if key:
        link(track_id, key, conn)
    return key


def grid_for_track(track_id: int, conn: Connection) -> Optional[Grid]:
    """The stored grid for a track, at its stored resolution."""
    row = conn.execute(
        "SELECT a.cols, a.rows, a.pixels FROM track_art t"
        " JOIN cover_art a ON a.art_key = t.art_key WHERE t.track_id = %s",
        (track_id,)).fetchone()
    if row is None:
        return None
    return unpack(row["pixels"], row["cols"], row["rows"])


def render_for_track(track_id: int, cols: int, rows: int,
                     conn: Connection, *, pad_to: int = 0):
    """Stored art fitted to a panel, or None.

    The point of the whole module: this keeps working after the source URL has
    stopped resolving.
    """
    from . import coverart

    grid = grid_for_track(track_id, conn)
    if grid is None:
        return None
    # Fit the stored shape into the panel rather than filling it: the grid has
    # its own aspect now, and stretching it here would undo the fitting done
    # when it was captured.
    have = (len(grid[0]), len(grid))
    size = coverart.fit(have, cols, rows)
    if size is None:
        return None
    fitted = rescale(grid, size[0], size[1])
    if fitted is None:
        return None
    return coverart.to_text(fitted, pad_to=pad_to)


def stats(conn: Connection) -> dict[str, Any]:
    """How much art is kept, and how much sharing it is doing."""
    row = conn.execute(
        "SELECT count(*) AS covers, COALESCE(sum(length(pixels)), 0) AS bytes"
        " FROM cover_art").fetchone()
    linked = conn.execute("SELECT count(*) AS n FROM track_art").fetchone()["n"]
    return {"covers": row["covers"], "bytes": row["bytes"], "tracks": linked}
