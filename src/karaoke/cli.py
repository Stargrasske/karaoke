"""Console entrypoints: karaoke, lyricsearch, music-index."""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

from .identify import SongRef, from_file, identify_live, parse_query
from .player import DEFAULT_LEAD_S


def _resolve(args) -> Optional[SongRef]:
    if args.file:
        return from_file(args.file)
    if args.youtube:
        from . import youtube
        return youtube.resolve_youtube(args.youtube, download=args.download)
    if args.spotify:
        from .spotify_client import SpotifyClient
        pb = SpotifyClient().current_playback()
        if not pb or not pb.title:
            print("Nothing playing on Spotify. Start a track first.", file=sys.stderr)
            return None
        print(f"Spotify: {pb.artist} - {pb.title}", file=sys.stderr)
        return SongRef(artist=pb.artist, title=pb.title,
                       duration=pb.duration_ms / 1000.0, source="spotify")
    if args.player:
        from .playerctl import current_songref
        ref = current_songref()
        if ref is None:
            print("No player active or metadata available via playerctl.", file=sys.stderr)
            return None
        print(f"Player: {ref.artist} - {ref.title}", file=sys.stderr)
        return ref
    if args.listen or args.output:
        print(f"Listening ({'output' if args.output else 'mic'})...", file=sys.stderr)
        ref = identify_live(mic=not args.output, timeout=args.timeout)
        if ref:
            print(f"Identified: {ref.artist} - {ref.title}", file=sys.stderr)
        return ref
    if args.query:
        return parse_query(" ".join(args.query))
    return None


def karaoke_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke` CLI for lookup, printing, playback and live sync modes."""
    ap = argparse.ArgumentParser(prog="karaoke", description="Terminal karaoke with synced lyrics")
    ap.add_argument("query", nargs="*", help="'Artist - Title' (or a title)")
    ap.add_argument("--file", "-f", help="local audio file (read tags)")
    ap.add_argument("--listen", "-l", action="store_true", help="identify room audio via mic")
    ap.add_argument("--output", "-o", action="store_true", help="identify laptop output audio")
    ap.add_argument("--youtube", "-y", metavar="URL",
                    help="karaoke a YouTube video URL (yt-dlp metadata -> lyrics)")
    ap.add_argument("--download", action="store_true",
                    help="with --youtube: download audio so Whisper/beats can run")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="with --youtube: use a logged-in browser's cookies for "
                         "Premium-quality/library access (e.g. firefox, chrome, "
                         "'firefox:PROFILE')")
    ap.add_argument("--cookies", metavar="FILE",
                    help="with --youtube: cookies.txt for authenticated access")
    ap.add_argument("--yt-cache-max-mb", type=int, default=None, metavar="MB",
                    help="with --youtube --download: auto-prune downloaded audio "
                         "cache after download (default KARAOKE_YT_CACHE_MAX_MB "
                         "or 500; 0 disables)")
    ap.add_argument("--spotify", "-s", action="store_true",
                    help="sync lyrics to the track currently playing on Spotify")
    ap.add_argument("--player", "-p", action="store_true",
                    help="get current song from any desktop player via playerctl (MPRIS)")
    ap.add_argument("--player-follow", action="store_true",
                    help="continuously follow desktop player via playerctl (MPRIS)")
    ap.add_argument("--player-sync", action="store_true",
                    help="continuously sync to desktop player position via playerctl (MPRIS)")
    ap.add_argument("--radio", "-r", action="store_true",
                    help="continuously follow live audio (mic): re-identify + re-sync as songs change")
    ap.add_argument("--record", "-R", "--rec", dest="record", action="store_true",
                    help="record live audio alongside radio mode (or sample capture)")
    ap.add_argument("--radio-sessions", action="store_true",
                    help="list recent radio listening sessions and their recording/import status")
    ap.add_argument("--import-radio", type=int, metavar="SESSION_ID",
                    help="import captured tracks from a radio session into the library")
    ap.add_argument("--reidentify", type=float, default=30.0,
                    help="seconds between re-identifications in --radio mode (default 30)")
    ap.add_argument("--timeout", "-t", type=int, default=30, help="listen timeout secs")
    ap.add_argument("--no-cache", action="store_true", help="skip OpenSearch cache, always fetch")
    ap.add_argument("--transcribe", action="store_true",
                    help="if no LRCLIB lyrics, transcribe a local --file with Whisper")
    ap.add_argument("--force-transcribe", action="store_true",
                    help="always transcribe --file with Whisper (skip cache + LRCLIB)")
    ap.add_argument("--lyrics-file", help="with --force-transcribe, use this text file "
                                           "for the lyrics instead of Whisper's ASR")
    ap.add_argument("--offset", type=float, default=0.0, help="lyric clock offset secs")
    ap.add_argument("--lead", type=float, default=None,
                    help="forward pre-bias (secs) for mic/radio sync to offset the "
                         f"~10s recognition window (default {DEFAULT_LEAD_S}; use 0 to disable)")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print lyrics instead of the live player")
    ap.add_argument("--no-beats", action="store_true",
                    help="skip librosa beat detection in --file mode (use per-line pulse)")
    args = ap.parse_args(argv)

    if getattr(args, "radio_sessions", False):
        from .radio_pipeline import print_radio_sessions
        return print_radio_sessions()

    if getattr(args, "import_radio", None) is not None:
        from .radio_pipeline import run_cli_import_radio_session
        return run_cli_import_radio_session(args.import_radio)

    # Forward pre-bias for live recognition (mic/radio) modes only. Spotify has an
    # exact position and text/file modes start from a keypress, so no lead there.
    lead = DEFAULT_LEAD_S if args.lead is None else args.lead

    # Continuous radio mode: self-contained loop, no single-song resolution.
    if args.radio:
        from .player import play_radio_synced
        play_radio_synced(mic=not args.output, record=args.record,
                          reidentify_interval=args.reidentify,
                          extra_latency=args.offset + lead, listen_timeout=args.timeout)
        return 0

    # Continuous Spotify mode: follow playback track-by-track. Tracks with no
    # synced lyrics are skipped (wait for the next song) instead of exiting.
    # (--print falls through to the one-shot resolve+dump path below.)
    if args.spotify and not args.print_only:
        from .player import play_spotify_loop
        play_spotify_loop(offset=args.offset, use_cache=not args.no_cache)
        return 0

    if args.player_follow:
        from .player_follow import play_playerctl_follow
        play_playerctl_follow(use_cache=not args.no_cache)
        return 0

    if args.player_sync:
        from .player_sync import play_synced_to_player
        play_synced_to_player(use_cache=not args.no_cache)
        return 0

    ref = _resolve(args)
    if not ref or not ref.title:
        ap.error("no song given. Use --file, --listen, or 'Artist - Title'.")

    from .player import get_synced, timeline_from_lyrics, render_lines, play

    stats_mode = ref.source  # file | query | songrec | spotify
    if args.listen:
        stats_mode = "listen"
    elif args.output:
        stats_mode = "output"

    ly = get_synced(ref,
                    use_cache=not args.no_cache,
                    transcribe=args.transcribe or args.force_transcribe,
                    force_transcribe=args.force_transcribe,
                    lyrics_file=args.lyrics_file,
                    stats_mode=stats_mode)
    tl = timeline_from_lyrics(ly)

    if not args.print_only:
        from . import localcache
        localcache.log_event(
            stats_mode, "play", artist=ref.artist, title=ref.title,
            source=ly.source, has_synced=bool(tl.lines),
        )

    if not tl.lines:
        player_url = getattr(ref, "url", None)
        if player_url and ("youtube.com/" in player_url or "youtu.be/" in player_url):
            print(f"No synced lyrics for {ref.artist} - {ref.title} (source={ly.source}).", file=sys.stderr)
            print(f"To stage lyrics from YouTube captions, run:", file=sys.stderr)
            print(f"  karaoke-stage youtube '{player_url}'", file=sys.stderr)
            return 2
        print(f"No synced lyrics for {ref.artist} - {ref.title} (source={ly.source}).", file=sys.stderr)
        
        if stats_mode in ("radio", "player", "query"):
            from . import localcache
            try:
                with localcache.connect() as conn:
                    localcache.log_lyric_gap(ref.artist, ref.title, conn)
            except Exception:
                pass

        if ly.plain:
            print("\n--- plain lyrics ---\n" + ly.plain)
            return 0
        return 2

    if args.print_only:
        for t, text in tl.lines:
            print(f"[{int(t//60):02d}:{t%60:05.2f}] {text}")
        return 0

    # Live mic/output identification returns a position offset -> auto-sync to it.
    if ref.offset is not None and ref.offset_mono is not None:
        from .player import play_offset_synced
        play_offset_synced(tl, title=ref.title, artist=ref.artist,
                           offset=ref.offset, offset_mono=ref.offset_mono,
                           extra_latency=args.offset + lead)
        return 0

    # File/text mode: if we have a local audio file, detect real beats so the
    # display flashes on the beat (needs librosa; degrades to per-line pulse).
    beat_times = None
    if ref.path and not args.no_beats:
        from .beats import detect_beats
        print("Detecting beats (first run may take a moment)…", file=sys.stderr)
        bpm, beat_times = detect_beats(ref.path)
        if beat_times:
            print(f"Beat track: {bpm:.0f} BPM, {len(beat_times)} beats.", file=sys.stderr)
        else:
            print("No beats detected (librosa missing or unreadable audio); "
                  "using per-line pulse.", file=sys.stderr)

    play(tl, title=ref.title, artist=ref.artist, offset=args.offset,
         beat_times=beat_times)
    return 0


def karaoke_yt_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-yt` CLI: karaoke a YouTube URL directly.

    A thin, friendlier front-end over ``karaoke --youtube``: the URL is the
    positional argument (no flag needed) and it translates the download/print/
    transcribe/offset options into a ``karaoke_main`` invocation, so the whole
    downstream lyrics -> render pipeline is reused with zero duplication.
    """
    ap = argparse.ArgumentParser(
        prog="karaoke-yt",
        description="Karaoke a YouTube video URL (yt-dlp metadata -> synced lyrics)",
    )
    ap.add_argument("url", nargs="?", help="YouTube video URL")
    ap.add_argument("--download", "-d", action="store_true",
                    help="download audio so Whisper/beats can run (unlike Spotify)")
    ap.add_argument("--transcribe", action="store_true",
                    help="if no LRCLIB lyrics, transcribe downloaded audio with Whisper "
                         "(implies --download)")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print lyrics instead of the live player")
    ap.add_argument("--no-cache", action="store_true",
                    help="skip caches, always fetch fresh")
    ap.add_argument("--no-beats", action="store_true",
                    help="skip librosa beat detection on downloaded audio")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="use a logged-in browser's cookies for Premium-quality/"
                         "library access (e.g. firefox, chrome, 'firefox:PROFILE')")
    ap.add_argument("--cookies", metavar="FILE",
                    help="cookies.txt for authenticated (Premium/library) access")
    ap.add_argument("--cache-status", action="store_true",
                    help="show YouTube audio download-cache size and exit")
    ap.add_argument("--clear-cache", action="store_true",
                    help="delete all downloaded YouTube audio and exit")
    ap.add_argument("--prune-cache", type=int, metavar="MB",
                    help="prune downloaded YouTube audio to MB and exit")
    ap.add_argument("--cache-max-mb", type=int, default=None, metavar="MB",
                    help="auto-prune downloaded audio after this run (default "
                         "KARAOKE_YT_CACHE_MAX_MB or 500; 0 disables)")
    ap.add_argument("--offset", type=float, default=0.0, help="lyric clock offset secs")
    args = ap.parse_args(argv)

    from .youtube import clear_youtube_cache, prune_youtube_cache, youtube_cache_summary

    if args.cache_status:
        s = youtube_cache_summary()
        print(f"YouTube cache: {s.files} files, {s.mib:.1f} MiB at {s.directory}")
        return 0
    if args.clear_cache:
        s = clear_youtube_cache()
        print(f"Cleared YouTube cache: removed {s.removed_files} files "
              f"({s.removed_mib:.1f} MiB) from {s.directory}")
        return 0
    if args.prune_cache is not None:
        s = prune_youtube_cache(args.prune_cache)
        print(f"Pruned YouTube cache to <= {args.prune_cache} MiB: removed "
              f"{s.removed_files} files ({s.removed_mib:.1f} MiB); "
              f"now {s.files} files, {s.mib:.1f} MiB at {s.directory}")
        return 0

    if not args.url:
        ap.error("url is required unless using --cache-status/--clear-cache/--prune-cache")

    # Transcription needs local audio, so it forces a download.
    download = args.download or args.transcribe

    forwarded: list[str] = ["--youtube", args.url]
    if download:
        forwarded.append("--download")
    if args.transcribe:
        forwarded.append("--transcribe")
    if args.print_only:
        forwarded.append("--print")
    if args.no_cache:
        forwarded.append("--no-cache")
    if args.no_beats:
        forwarded.append("--no-beats")
    if args.cookies_from_browser:
        forwarded += ["--cookies-from-browser", args.cookies_from_browser]
    if args.cookies:
        forwarded += ["--cookies", args.cookies]
    if args.cache_max_mb is not None:
        forwarded += ["--yt-cache-max-mb", str(args.cache_max_mb)]
    if args.offset:
        forwarded += ["--offset", str(args.offset)]

    return karaoke_main(forwarded)


def lyricsearch_main() -> int:
    """Run the `lyricsearch` CLI for semantic or keyword lyric search."""
    ap = argparse.ArgumentParser(prog="lyricsearch",
                                 description="Find a song by what its lyrics mean")
    ap.add_argument("query", nargs="+", help="words / a phrase from the song")
    ap.add_argument("-k", type=int, default=5, help="number of results")
    ap.add_argument("--keyword", action="store_true", help="keyword search instead of semantic")
    args = ap.parse_args()

    from .search import keyword_search, semantic_search

    q = " ".join(args.query)
    hits = keyword_search(q, k=args.k) if args.keyword else semantic_search(q, k=args.k)
    if not hits:
        print("no matches", file=sys.stderr)
        return 1
    for h in hits:
        mark = "♪" if h.has_synced else " "
        print(f"{h.score:6.3f} {mark} {h.artist} - {h.title}  "
              f"[{h.source}{'/'+h.album if h.album else ''}]")
    return 0


def index_main() -> int:
    """Run the `music-index` CLI to scan local audio into OpenSearch."""
    ap = argparse.ArgumentParser(prog="music-index",
                                 description="Scan a music library into OpenSearch")
    ap.add_argument("--dir", help="music dir (default MUSIC_DIR)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if indexed")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    from pathlib import Path
    from .scanner import scan

    stats = scan(Path(args.dir) if args.dir else None,
                 force=args.force, limit=args.limit)
    print(f"\nseen={stats.seen} indexed={stats.indexed} skipped={stats.skipped} "
          f"synced={stats.with_synced} errors={stats.errors}")
    return 0


def stats_main() -> int:
    """Run the `karaoke-stats` CLI to report play and radio-discovery stats."""
    ap = argparse.ArgumentParser(
        prog="karaoke-stats",
        description="Play counts and radio-discovery stats from the local cache",
    )
    ap.add_argument("-n", "--limit", type=int, default=10, help="rows in top lists")
    ap.add_argument("--days", type=float, default=None,
                    help="only count events in the last N days")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    import time as _time
    from . import localcache
    from .api_client import ApiClient

    # Prefer the Library API so `karaoke-stats` and a web UI read identical
    # numbers; fall back to a direct SQLite summary when no server is running.
    data = ApiClient().get_stats(limit=args.limit, days=args.days)
    if data is None:
        since = _time.time() - args.days * 86400 if args.days else None
        s = localcache.summarize(limit=args.limit, since=since)
        data = {
            "plays": s.plays, "discoveries": s.discoveries,
            "cache_hits": s.cache_hits, "cache_misses": s.cache_misses,
            "cache_hit_rate": s.cache_hit_rate,
            "distinct_tracks": s.distinct_tracks, "distinct_artists": s.distinct_artists,
            "top_tracks": [list(t) for t in s.top_tracks],
            "top_artists": [list(a) for a in s.top_artists],
            "by_mode": [list(m) for m in s.by_mode],
            "total_events": s.total_events,
        }

    if args.json:
        import json
        print(json.dumps({
            "total_events": data["total_events"], "plays": data["plays"],
            "discoveries": data["discoveries"], "cache_hits": data["cache_hits"],
            "cache_misses": data["cache_misses"],
            "cache_hit_rate": round(data["cache_hit_rate"], 3),
            "distinct_tracks": data["distinct_tracks"],
            "distinct_artists": data["distinct_artists"],
            "top_tracks": [{"artist": t[0], "title": t[1], "plays": t[2]} for t in data["top_tracks"]],
            "top_artists": [{"artist": a[0], "plays": a[1]} for a in data["top_artists"]],
            "by_mode": [{"mode": m[0], "plays": m[1]} for m in data["by_mode"]],
        }, indent=2))
        return 0

    window = f" (last {args.days:g} days)" if args.days else ""
    print(f"Karaoke stats{window}")
    print(f"  plays={data['plays']}  discoveries={data['discoveries']}  "
          f"tracks={data['distinct_tracks']}  artists={data['distinct_artists']}")
    print(f"  local-cache hits={data['cache_hits']}  misses={data['cache_misses']}  "
          f"hit-rate={data['cache_hit_rate']*100:.0f}%")
    if data["by_mode"]:
        print("\n  by mode:")
        for mode, n in data["by_mode"]:
            print(f"    {mode:10s} {n}")
    if data["top_tracks"]:
        print("\n  top tracks:")
        for t in data["top_tracks"]:
            a, title, n = t[0], t[1], t[2]
            label = f"{a} - {title}" if a else title
            print(f"    {n:4d}  {label}")
    if data["top_artists"]:
        print("\n  top artists:")
        for a, n in data["top_artists"]:
            print(f"    {n:4d}  {a}")
    if data["total_events"] == 0:
        print("  (no events yet — play something with `karaoke` first)")
    return 0


def stage_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-stage` CLI for unapproved lyrics candidates."""
    ap = argparse.ArgumentParser(
        prog="karaoke-stage",
        description="Stage/review lower-trust lyrics before approving them into the local cache",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    yt = sub.add_parser("youtube", help="stage YouTube captions as unapproved lyrics")
    yt.add_argument("url", help="YouTube URL")
    yt.add_argument("--language", "-l", action="append", dest="languages",
                    help="caption language preference (repeatable; default en,nl)")
    yt.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="use logged-in browser cookies for caption access")
    yt.add_argument("--cookies", metavar="FILE", help="cookies.txt for authenticated access")

    cap = sub.add_parser("captions",
                         help="check whether a YouTube video has lyric captions")
    cap.add_argument("url", help="YouTube URL")
    cap.add_argument("--language", "-l", action="append", dest="languages",
                     help="caption language preference (repeatable; default en,en-orig,nl)")
    cap.add_argument("--cookies-from-browser", metavar="BROWSER",
                     help="use logged-in browser cookies for caption access")
    cap.add_argument("--cookies", metavar="FILE", help="cookies.txt for authenticated access")

    ls = sub.add_parser("list", help="list staged lyric candidates")
    ls.add_argument("--status", default="pending", choices=["pending", "approved", "rejected", "all"])
    ls.add_argument("-n", "--limit", type=int, default=20)

    show = sub.add_parser("show", help="show one staged candidate")
    show.add_argument("id", type=int)

    approve = sub.add_parser("approve", help="approve staged lyrics into the local cache")
    approve.add_argument("id", type=int)

    reject = sub.add_parser("reject", help="reject staged lyrics")
    reject.add_argument("id", type=int)

    args = ap.parse_args(argv)

    if args.cmd == "captions":
        from .stage_sources import check_youtube_captions
        avail = check_youtube_captions(
            args.url,
            languages=args.languages or ("en", "en-orig", "nl"),
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies,
        )
        print(f"captions: {avail.describe()}")
        print(f"  manual   : {', '.join(avail.manual_languages) or '-'}")
        print(f"  automatic: {', '.join(avail.automatic_languages[:8]) or '-'}")
        if avail.best is not None and avail.best.ext == "json3":
            print("  -> json3 available: can produce SYNCED lyrics from cue timings")
        elif avail.available:
            print("  -> no json3 track: would produce PLAIN lyrics only")
        return 0 if avail.available else 1

    if args.cmd == "youtube":
        from .stage_sources import stage_youtube_captions
        result = stage_youtube_captions(
            args.url,
            languages=args.languages or ("en", "nl"),
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies,
        )
        print(
            f"staged #{result.staged_id}: {result.artist} - {result.title} "
            f"({result.source_kind}, {result.lines} lines)"
        )
        print("review with: karaoke-stage show", result.staged_id)
        print("approve with: karaoke-stage approve", result.staged_id)
        return 0

    from . import staging

    if args.cmd == "list":
        items = staging.list_staged(status=args.status, limit=args.limit)
        if not items:
            print("no staged lyrics")
            return 0
        for item in items:
            synced = "♪" if item.has_synced else " "
            print(
                f"{item.id:4d} {synced} {item.status:8s} "
                f"{item.source_kind:24s} {item.artist} - {item.title}"
            )
        return 0

    if args.cmd == "show":
        item = staging.get_staged(args.id)
        if item is None:
            print(f"no staged lyrics with id {args.id}", file=sys.stderr)
            return 1
        print(f"#{item.id} {item.status}: {item.artist} - {item.title}")
        print(f"source={item.source_kind} confidence={item.confidence:.2f} url={item.source_url}")
        if item.notes:
            print(f"notes={item.notes}")
        print("\n--- synced lyrics ---")
        print(item.synced_lyrics or "(none)")
        print("\n--- plain lyrics ---")
        print(item.plain_lyrics or "(none)")
        return 0

    if args.cmd == "approve":
        item = staging.approve_staged(args.id)
        print(f"approved #{item.id} into local lyrics cache: {item.artist} - {item.title}")
        return 0

    if args.cmd == "reject":
        item = staging.reject_staged(args.id)
        print(f"rejected #{item.id}: {item.artist} - {item.title}")
        return 0

    return 1


def player_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-player` CLI to control desktop media players."""
    ap = argparse.ArgumentParser(
        prog="karaoke-player",
        description="Control desktop media players via MPRIS (playerctl)",
    )
    ap.add_argument("--player", "-p", help="target a specific player by name")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("play", help="send the play command")
    sub.add_parser("pause", help="send the pause command")
    sub.add_parser("play-pause", help="toggle play/pause")
    sub.add_parser("next", help="go to the next track")
    sub.add_parser("previous", help="go to the previous track")
    sub.add_parser("stop", help="send the stop command")
    sub.add_parser("status", help="get the current player status")

    seek = sub.add_parser("seek", help="seek to a position in the current track")
    seek.add_argument("position", help="position in seconds, or +/- offset")

    args = ap.parse_args(argv)

    try:
        cmd = ["playerctl"]
        if args.player:
            cmd.extend(["--player", args.player])
            
        if args.cmd == "seek":
            cmd.extend(["position", args.position])
        else:
            cmd.append(args.cmd)
        subprocess.run(cmd, check=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        print("playerctl command failed or not found. Is it installed?", file=sys.stderr)
        return 1
        
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(karaoke_main())

def backfill_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-backfill` CLI to fill lyric gaps."""
    from .backfill import backfill_main as _backfill_main
    return _backfill_main(argv)


def folder_scan_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-folder-scan` CLI.

    Scans a music folder, fingerprints with Shazam/songrec, classifies audio
    (key/BPM/CLAP/genre), resolves YouTube/Spotify links, and ingests into the
    library. The heavy lifting lives in :mod:`karaoke.folder_scan`, which is the
    same code the control API's ``/api/scan/folder`` calls.
    """
    import argparse
    from pathlib import Path

    from .folder_scan import scan_and_ingest_folder

    ap = argparse.ArgumentParser(
        prog="karaoke-folder-scan",
        description="Scan a music folder, fingerprint, classify, resolve "
                    "YT/Spotify, and ingest into the library",
    )
    ap.add_argument("dir", type=Path, help="Folder containing music files")
    ap.add_argument("--no-fingerprint", action="store_true",
                    help="disable songrec Shazam fingerprinting")
    ap.add_argument("--no-classify", action="store_true",
                    help="disable key/BPM/CLAP/genre classification")
    ap.add_argument("--no-streaming", action="store_true",
                    help="disable YouTube/Spotify link resolution")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview enrichment without writing to the library")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N files")
    args = ap.parse_args(argv)

    stats = scan_and_ingest_folder(
        args.dir,
        use_fingerprint=not args.no_fingerprint,
        classify_audio=not args.no_classify,
        resolve_streaming=not args.no_streaming,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    if args.dry_run:
        for item in stats["items"]:
            print(f"  {item['artist']} - {item['title']}  "
                  f"[key={item['key']} bpm={item['bpm']} genre={item['genre']}]")
    print(f"\nseen={stats['seen']} processed={stats['processed']} "
          f"fingerprinted={stats['fingerprinted']} classified={stats['classified']} "
          f"sourced={stats['sourced']} errors={stats['errors']}")
    return 0


def browse_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-browse` TUI."""
    from .browse import browse_main as _browse_main
    return _browse_main()


def tui_main(argv: Optional[list[str]] = None) -> int:
    """Run the player-aware `karaoke-tui` interface."""
    from .tui import tui_main as _tui_main
    return _tui_main()


def analyze_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-analyze` CLI: detect + store key/BPM, verify keys.

    Examples:
      karaoke-analyze --file song.webm --artist Ren --title "Hi Ren"
      karaoke-analyze --verify --artist Ren --title "Hi Ren" --key "C major"
      karaoke-analyze --list
    """
    from .logger import stream_logs

    ap = argparse.ArgumentParser(
        prog="karaoke-analyze",
        description="Detect/store musical key + tempo and verify keys online",
    )
    ap.add_argument("--file", "-f", help="local audio file to analyse")
    ap.add_argument("--artist", default="", help="track artist (for DB storage)")
    ap.add_argument("--title", default="", help="track title (for DB storage)")
    ap.add_argument("--verify", action="store_true",
                    help="reconcile the stored detected key with --key")
    ap.add_argument("--key", help="reference/online key for --verify (e.g. 'C major')")
    ap.add_argument("--reference-source", default="online",
                    help="where --key came from (default: online)")
    ap.add_argument("--list", action="store_true", help="list stored analyses")
    ap.add_argument("--log", default="info",
                    help="console log level: off|err|info|full (default info)")
    args = ap.parse_args(argv)

    stream_logs(args.log)

    from . import localcache, track_analysis

    if args.list:
        with localcache.connect() as conn:
            track_analysis.ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT t.artist, t.title, a.detected_key, a.reference_key,
                       a.resolved_key, a.key_relation, a.bpm, a.key_confidence
                FROM track_analysis a JOIN tracks t ON t.track_id = a.track_id
                ORDER BY t.artist, t.title
                """
            ).fetchall()
        if not rows:
            print("no analyses stored yet")
            return 0
        for r in rows:
            print(
                f"{r['artist']} - {r['title']}: "
                f"detected={r['detected_key'] or '?'} "
                f"ref={r['reference_key'] or '-'} "
                f"resolved={r['resolved_key'] or '?'} "
                f"[{r['key_relation'] or '-'}] "
                f"bpm={r['bpm'] or '?'} conf={r['key_confidence'] or 0:.0%}"
            )
        return 0

    if args.verify:
        if not args.key or not (args.artist and args.title):
            ap.error("--verify needs --key, --artist and --title")
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(args.artist, args.title, conn)
            if track_id is None:
                print(f"track not found: {args.artist} - {args.title}", file=sys.stderr)
                return 1
            rec = track_analysis.verify_key(
                track_id, args.key, reference_src=args.reference_source, conn=conn
            )
        print(rec.note)
        print(f"relation={rec.relation} agree={rec.agree} "
              f"resolved={rec.resolved.name if rec.resolved else '?'}")
        return 0

    if not args.file:
        ap.error("give --file to analyse, or use --verify / --list")

    from .analyze import analyze_audio

    print(f"Analysing {args.file} …", file=sys.stderr)
    result = analyze_audio(args.file)
    key = result.key
    print(f"key: {key.name if key else 'unknown'} "
          f"(conf {result.key_confidence:.0%}, {result.key_agreement}, "
          f"{result.method})")
    print(f"bpm: {result.bpm if result.bpm else 'unknown'}")
    if result.energy is not None:
        print(f"energy: {result.energy:.2f}  brightness: "
              f"{result.brightness if result.brightness is not None else '?'}")

    if args.artist and args.title:
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(args.artist, args.title, conn)
            if track_id is None:
                from .lyrics import Lyrics
                localcache.add_track_and_lyrics(args.artist, args.title, Lyrics(),
                                                conn=conn)
                track_id = localcache.find_track_id(args.artist, args.title, conn)
            if track_id is not None:
                track_analysis.save_detected(
                    track_id,
                    detected_key=key,
                    key_confidence=result.key_confidence,
                    key_agreement=result.key_agreement,
                    bpm=result.bpm,
                    method=result.method,
                    energy=result.energy,
                    brightness=result.brightness,
                    analyzer_version=result.version,
                    source_kind="cli",
                    conn=conn,
                )
                print(f"stored analysis for track_id={track_id}", file=sys.stderr)
    return 0


