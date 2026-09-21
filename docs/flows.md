# Flows

Schemas for the runtime flows the TUI drives. Each is a small pipeline of pure
functions over external inputs (MPRIS, PipeWire, songrec, ffmpeg), kept
side-effect-light so the arithmetic can be tested without audio.

- [Detection flow](#detection-flow) — pick the active player and the karaoke mode.
- [Sample flow](#sample-flow) — key/BPM for one track by recording what plays.
- [Record flow](#record-flow) — unattended capture + marking, then offline decompile.
- [Lyric rescue flow](#lyric-rescue-flow) — words from the player's own panel, timings from a transcription.

Related: the [Architecture](architecture.md) page covers the lyric-lookup and
ingest flows; the per-mode pages under [Modes](modes/index.md) describe how each
of these is triggered and what the user sees.

## Detection flow

Every 1.5s the TUI asks the desktop what is playing and maps it to a mode. The
mic hints (`mic_artist`/`mic_title`) only ever break a tie between several
players that are *all* playing; they never invent a detection.

`detect.detect_active()` → `preferred_player()` → `classify()` → `Detection`.

```mermaid
flowchart TD
    A[detect_active mic_artist, mic_title] --> B[playerctl.playing_players]
    B --> C{how many playing?}
    C -- none --> Z[Detection mode=browse]
    C -- one --> D[classify that player]
    C -- several --> E[preferred_player]
    E --> E1{a player matches\nthe mic-identified track?}
    E1 -- yes --> D
    E1 -- no --> E2{any player name\nstarts with 'spotify'?}
    E2 -- yes --> D
    E2 -- no --> E3[first candidate] --> D
    D --> F{player name starts\nwith 'spotify'?}
    F -- yes --> G[Detection mode=spotify]
    F -- no --> H{has title, artist or url?}
    H -- yes --> I[Detection mode=scan]
    H -- no --> Z
```

Why the tie-break order is fixed: without a stable rule the choice follows
playerctl's listing order and the mode flaps between players mid-song. Priority
is (1) the player whose track the microphone just identified — provably the one
making the sound in the room, (2) Spotify, which reports an exact position where
a browser tab does not, (3) whatever came first, so there is always an answer.

Chrome names its player per instance (`chromium.instance402904`), so only a
prefix comparison is stable across restarts.

### Lyric resolution for a detection

Once a mode is chosen, `detect.resolve_lyrics()` turns it into
`(artist, title, lyrics)` from the local SQLite cache, preferring the canonical
URL over the often-stale browser artist/title.

```mermaid
flowchart TD
    A[resolve_lyrics detection] --> B{detection has a url?}
    B -- yes --> C[find_track_by_url\nsources table]
    C -- hit --> Z[return cached lyrics]
    B -- no / miss --> D{artist or title?}
    D -- no --> Y[return None]
    D -- yes --> E[find_track_id exact]
    E -- hit --> Z
    E -- miss --> F["retry with clean_title\ndrop '(Remastered)' etc."]
    F -- hit --> Z
    F -- miss --> G[find_track_id_relaxed\nfull credit / decorated title]
    G -- hit --> Z
    G -- miss --> H{empty artist\nbut have title?}
    H -- yes --> I[title-only lookup\nprefer rows with synced lyrics]
    I -- hit --> Z
    H -- no --> Y
```

A miss on a real `(artist, title)` is persisted by `record_gap()`: the source
(track + URL) is stored when the title is real, and a lyric gap is logged for
`karaoke-backfill` / `karaoke-stage` — but only when **both** artist and title
are known, since an artist-less row could never be matched against LRCLIB and
would sit in the queue being retried forever.

## Sample flow

`k` in the TUI (or `karaoke-sample` / `make sample`). Post-processing needs
audio; a Spotify-only track has no downloadable file, so instead the sink
**monitor** is recorded in real time — a clean digital copy of exactly what is
playing, no microphone and no room noise. Capture is real time: 45s of audio
takes 45s, which is why it runs for the one track in front of you rather than
over a backlog.

`sample_audio.sample_and_analyse()` → `capture()` → `analyse_sample()` →
`analyze.analyze_audio()` → `track_analysis.save_detected()`.

```mermaid
flowchart TD
    A[sample_and_analyse artist, title, seconds=45] --> B{seconds >= 20?}
    B -- no --> E1[CaptureError: too short]
    B -- yes --> C[playing_sink → monitor_source]
    C --> C1{a monitor to record?}
    C1 -- no --> E2[CaptureError: nothing playing]
    C1 -- yes --> D[ffmpeg -f pulse -i SINK.monitor\n-t seconds → temp .wav]
    D --> F[analyze_audio wav]
    F --> G{key and bpm both None?}
    G -- yes --> E3[AnalysisUnavailable\nmake install-audio]
    G -- no --> H{artist and title given?}
    H -- no --> R[return result only]
    H -- yes --> I[find_track_id\nor create track row]
    I --> J[save_detected\nmethod = essentia-edma-vote+sample]
    J --> K[delete temp wav unless --keep]
```

The `+sample` method suffix marks the analysis as excerpt-derived wherever it is
displayed, so a sampled result is never mistaken for a full-track one. Key is a
global property and survives excerpting well; tempo is reliable for steady
material and less so where the track changes tempo.

`playing_sink()` is deliberately *not* the default sink: with a Bluetooth
speaker paired alongside built-in speakers, the default is regularly not where
the music is routed, so the sink actually carrying a stream wins.

### What `analyze_audio` does

The key/tempo analyzer degrades gracefully and delegates when the heavy DSP
stack (essentia, librosa) is not in the current interpreter.

```mermaid
flowchart TD
    A[analyze_audio path] --> B{stack_available here?}
    B -- no --> C{audio_python found?\n.venv-audio or KARAOKE_AUDIO_PYTHON}
    C -- yes --> D[run out-of-process\npython -m karaoke.analyze --json]
    D -- ok --> Z[AudioAnalysis]
    C -- no --> E[transcode to wav if needed]
    B -- yes --> E
    D -- fail --> E
    E --> F[detect_key: Essentia edma,\nvoted over 6 windows]
    E --> G[detect_features: librosa\nbpm + energy + brightness]
    F --> Z
    G --> Z
```

Key detection votes across the full track plus several windows (intro, ~35%,
~70%, seeded random slices). The **windows are authoritative** — the full-track
read is kept only for reporting and the no-window fallback, because on the
benchmark it is wrong on ~half the tracks and must never override a window
consensus.

## Record flow

`O` in the TUI (or the `karaoke-recording` / `make recordings` family). The
unattended version of sampling: leave it running for an evening and it captures
everything coming out of the speakers while, in parallel, asking songrec every
so often what is playing. The recording is a means to metadata, not a library —
the audio is discarded once analysed unless `keep_audio` is set.

Two independent readers of the same monitor is fine (PipeWire allows it), so
ffmpeg and songrec do not contend, and the monitor is a different device from
the microphone, so record mode composes with radio mode rather than fighting it
for an input.

### Capture

`recorder.start()` spawns a segmenting ffmpeg and an identification thread.

```mermaid
flowchart TD
    A[recorder.start source] --> B[INSERT recordings\nstatus=recording]
    B --> C[ffmpeg -f pulse -i monitor\n-f segment 600s FLAC\nseg-%Y%m%d-%H%M%S.flac]
    B --> D[identify thread]
    subgraph loop[every ~45s until stop]
        D --> D1{ffmpeg still alive?}
        D1 -- no --> D2[finish failed → forget]
        D1 -- yes --> D3{over 8h / 6GB cap?}
        D3 -- yes --> D4[stop]
        D3 -- no --> D5[identify_live mic=false\nsource=monitor]
        D5 --> D6[add_mark: at_wall, at_offset,\nartist, title, ok]
        D6 --> D7{3+ consecutive misses?}
        D7 -- yes --> D8[back off x4]
        D7 -- no --> D5
    end
    E[recorder.stop] --> F[SIGTERM ffmpeg\nfinalise current segment]
    F --> G[UPDATE recordings\nstatus=complete]
```

Segments are named for the wall-clock instant they were opened (`-strftime`),
so the timeline survives a crash: audio can be located from a marker with no
extra bookkeeping and with no dependence on the recorder still running. ffmpeg
gets SIGTERM, not SIGKILL, so it finalises the FLAC it is writing rather than
leaving it headerless and losing the tail.

Failed identifications are stored, not dropped: a gap in the markers is evidence
about the recording (silence, speech, an unknown track) and discarding it would
make the timeline look continuous when it is not.

### Decompile

`recording_worker.analyse()` turns markers back into tracks and analyses each
confident one at full speed. It runs automatically when a recording is stopped
with `O`; the CLI and the control API drive the same function by hand. The
marker maths lives in `recording_slice` as pure functions over markers, testable
without any audio.

The key move: a marker doesn't merely name a track, it *dates* it —
`start_wall = at_wall - at_offset`. Every marker of the same track yields an
independent estimate of when it began, and those should agree. Agreement is
confirmation; disagreement means the track changed, repeated, was seeked, or
songrec matched a different release — so wide-spread segments are gated out of
automatic analysis.

```mermaid
flowchart TD
    A[analyse recording_id] --> B[segment_files:\nplace FLACs on the wall clock]
    B --> C[recording_span: range captured]
    C --> D[load_marks → segments]
    subgraph seg[per segment]
        D --> E{is_confident?\n>=2 marks, spread <=4s, dur>0}
        E -- no --> E1[skip: report spread\nfor manual review]
        E -- yes --> F[clamp to captured span]
        F -- too little --> F1[skip: too little audio]
        F -- ok --> G[cut: concat overlapping\nsegments, ffmpeg -ss/-t]
        G --> H[analyze_audio wav]
        H --> I[save_detected\nmethod = ...+recording]
        I --> J[index audio vector\nif OpenSearch reachable]
    end
    J --> K[UPDATE status=analysed]
    K --> L{keep_audio?}
    L -- no --> M[discard FLACs, keep markers]
    L -- yes --> N[keep audio in dir]
```

`group_marks()` splits markers into consecutive runs of the same track; a failed
identification **ends** a run rather than being skipped over, because silence or
an unrecognised track between two matches of the same song is real evidence they
are two separate plays. `segment_from()` takes the **median** of the per-marker
start estimates, never the mean, so a single bad match cannot drag the boundary.

Segment start times are on the *track's* timeline, so a song identified partway
through is dated from before the recording began — slices are therefore clamped
to the span actually captured, and a segment with too little audio left after
clamping is skipped rather than analysed from a fragment. Audio also routinely
straddles two 10-minute FLAC files, so overlapping segments are concatenated
before the cut.

The `+recording` method suffix marks the analysis as recording-derived, so it is
never mistaken for one done on a downloaded master.

## Lyric rescue flow

For a track LRCLIB has never heard of. It can be playing with its full lyrics
on screen — YouTube Music's lyrics tab (SONGTEKST in Dutch), attributed to
LyricFind or Musixmatch — while the TUI beside it reports "no lyrics" and
queues the track for backfill. Captions are no help: those are a video's
subtitle track, a different thing from the lyrics panel, and most uploads have
none.

The panel has words and no timings. Whisper has the reverse. Joining them is
the whole flow.

### What you have to do for this to work

This reads what is **on screen**. It is not a service that can be queried, so
it only captures a track while that track is in front of you, and two things
are on the listener rather than the code:

1. **Open the song, not the album or a video.** The lyrics tab exists in song
   mode — the view for a single track. From an album or playlist view there is
   no panel to read, so nothing is captured however long the track plays.
2. **Open the lyrics tab itself.** The panel has to be rendered. YouTube Music
   does not populate that element until the tab has been selected, so a track
   played with the tab closed yields nothing.

With the tab open it works well and needs no further attention, but it does
need that once per track.

!!! tip "Play the audio release, not the video"

    Pick the audio version of a track rather than its video whenever both
    exist. Video edits carry intros, outros and edits the audio release does
    not, so the same song needs a different sync offset depending on which one
    played — and an offset saved against one is wrong for the other. It also
    affects the words: a live or video-only cut can differ from the studio
    lyric the panel supplies.

`_background_fetch_lyrics()` → `ytmusic_lyrics.for_playing()` →
`add_track_and_lyrics()` → `_enqueue_sync()` → the worker's `_run_sync()` →
`lyric_align.align_lyrics_to_lrc()`.

```mermaid
flowchart TD
    A[track playing, no cached lyrics or plain-only] --> B[fetch_lrclib<br/>clean_artist + clean_page_title]
    B -- synced found --> Z[store: lrclib synced]
    B -- nothing / plain only --> C[ytmusic_lyrics.read_panel<br/>over the existing CDP connection]
    C --> C1{panel present<br/>and >= 4 lines?}
    C1 -- no --> E1[give up: no words anywhere]
    C1 -- yes --> D{does the player agree<br/>it is this track?}
    D -- no --> E2[discard: the panel lags a track change]
    D -- yes --> F[store as plain<br/>NEVER downgrade existing synced]
    F --> G{has downloadable audio?}
    G -- no --> E3[stop: nothing to align against]
    G -- yes --> H[enqueue 'sync']
    subgraph worker[post-processing worker: 'sync']
        H --> H1{fast online check<br/>fetch_lrclib synced?}
        H1 -- yes --> H2[store: lrclib synced<br/>finished in ~100ms]
        H1 -- no --> I[_ensure_download: the whole track]
        I --> J[transcribe_to_words<br/>Whisper, timings only]
        J --> K[align_lyrics_to_lrc<br/>real words + Whisper rhythm]
        K --> L{any timestamps produced?}
        L -- no --> E4[not stored: an empty LRC<br/>would mark it done]
        L -- yes --> M[store: whisper_aligned]
    end
```

### Why each guard is there

**Never downgrade synced lyrics.** If SQLite already holds approved synced
lyrics for a track, an untimed plain text payload from the YouTube Music lyrics
panel or captions will never overwrite or erase the timestamps.

**The worker checks online before running Whisper.** Transcribing and aligning
with Whisper takes full audio downloads and heavy CPU/GPU time. The worker first
checks LRCLIB with normalized metadata; if real synced lyrics exist, it upgrades
instantly in ~100ms.

**The panel is verified against the player.** It lags a track change by a
moment, and attributing one song's words to another is invisible once written,
so `for_playing()` asks MPRIS what is playing and the two must agree.

**Whisper is used for timing only.** Its words on sung audio are unreliable —
"up to do" becomes "up to doom", plus musical-note artifacts — so where a real
source supplied the text, those words are kept and only the timestamps taken.
The same rule `upgrade_timings` follows for captions.

**Audio is required before queueing.** The panel answers for plenty of tracks
whose audio cannot be fetched. Queueing those would fail on every retry, the
way Spotify-only analysis did before `has_downloadable_source` gated it.

**Alignment needs the whole track**, not an excerpt: it spreads every line
across the full duration, so a 45s sample would compress them all into the
opening. That is why this goes to the worker rather than being sampled live
like [key/BPM](#sample-flow).

**An empty alignment is discarded.** Storing a blank LRC would mark the track
synced and stop anything ever trying again.

### The three lyric task types

`postprocess_queue.needs_postprocessing()` distinguishes them, and conflating
any two would make a track look handled when it is not:

| task | condition | what it does |
|---|---|---|
| `analysis` | no key/BPM row | detect key, tempo, energy |
| `sync` | lyrics are plain, no timings at all | **add** timings by alignment |
| `timings` | synced lyrics carry no word tags | **refine** to word level |

### Verified

End to end on *Dinosaur Jr. — "No One's Ready"*, which LRCLIB does not have:
43 lines read from the panel, a source found on the artist's own channel (195s
against the track's 194.5s), and alignment produced 1630 characters of LRC.

```
[00:12.60] no, don't fake me don't ya know no one's ready for your war
[00:21.16] what's it for
[00:25.88] born into a world of fire
```

Note that adding `sync` to the queue required **restarting the workers**: they
are long-running, and until they were restarted they accepted the task, ran the
`analysis` half they knew about, and silently skipped the rest.
