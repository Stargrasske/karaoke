# Architecture

Karaoke has three planes: data, ingest and client runtime.

```mermaid
flowchart LR
    subgraph Client[Host CLI]
        K[karaoke]
        S[lyricsearch]
        I[music-index]
    end

    subgraph Inputs[Music and identity inputs]
        Files[Local audio files\nMP3/FLAC/M4A/etc.]
        Spotify[Spotify playback API\ncurrent track + progress]
        Mic[Microphone or output monitor\nsongrec/Shazam fingerprint]
        Query[Artist - Title query]
    end

    subgraph Services[External/local services]
        LRCLIB[LRCLIB API\nplain + synced lyrics]
        Whisper[faster-whisper\nlocal transcription fallback]
        Embed[sentence-transformers\n384-dim vectors]
        OS[(OpenSearch tracks index\nmetadata + lyrics + vectors)]
    end

    Files --> I
    I -->|mutagen tags| LRCLIB
    I --> Embed
    I --> OS

    Query --> K
    Files --> K
    Spotify --> K
    Mic --> K
    K -->|cache read| OS
    K -->|cache miss| LRCLIB
    K -->|LRCLIB miss + local file| Whisper
    K -->|write-through lyrics cache| OS

    S --> Embed
    S -->|kNN or keyword query| OS
```

## Runtime components

| Module | Responsibility |
| --- | --- |
| `config` | Loads environment configuration and `.env` defaults. |
| `tags` | Extracts audio metadata using mutagen. |
| `lyrics` | Fetches LRCLIB lyrics and parses LRC timestamps. |
| `scanner` | Walks local music files and builds OpenSearch documents. |
| `spotify_import` | Converts Spotify library exports/API objects into metadata documents. |
| `embed` | Lazily loads the sentence-transformer model and creates normalized vectors. |
| `osclient` | Creates the OpenSearch client and ensures the `tracks` index/mapping exists. |
| `search` | Runs semantic, keyword and exact cache lookups. |
| `localcache` | Cluster-independent PostgreSQL store: lyrics cache, track metadata, analysis, play/discovery stats. |
| `identify` | Resolves songs from files, queries or live songrec matches. |
| `youtube` | Resolves a SongRef from a YouTube URL (yt-dlp metadata + smart title parser). |
| `whisper_sync` | Transcribes local audio into approximate LRC when LRCLIB has no synced lyrics. |
| `player` | Converts lyrics to timelines and renders Rich terminal karaoke views. |
| `beats` | Optional beat detection and fallback lyric-line pulse logic. |
| `sentiment` | Lightweight lyric mood classification for renderer tinting. |
| `cli` | Console entrypoints: `karaoke`, `lyricsearch`, `music-index`. |

## Main playback flow

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant CLI as karaoke CLI
    participant Identify as identify.py
    participant Cache as OpenSearch cache
    participant LRCLIB
    participant Whisper
    participant Player as player.py

    User->>CLI: karaoke "Artist - Title" / --file / --spotify / --listen / --youtube
    CLI->>Identify: resolve SongRef
    Identify-->>CLI: artist, title, optional offset/path
    CLI->>Cache: exact artist/title cache lookup
    alt synced lyrics cached
        Cache-->>CLI: synced_lyrics LRC
    else cache miss
        CLI->>LRCLIB: artist/title/album/duration lookup
        alt LRCLIB synced lyrics found
            LRCLIB-->>CLI: syncedLyrics
        else local file + --transcribe
            CLI->>Whisper: transcribe audio to LRC
            Whisper-->>CLI: approximate synced LRC
        end
        CLI->>Cache: write-through lyrics cache
    end
    CLI->>Player: LyricTimeline + sync mode
    Player-->>User: terminal lyrics with active line/word highlight
```

## Cache-first lyrics lookup

The cache is deliberately conservative: exact artist/title matching is used for lyrics lookup so a fuzzy OpenSearch match cannot return the wrong song's lyrics.

```mermaid
flowchart TD
    A[Need lyrics for artist/title] --> B{use_cache?}
    B -- yes --> C[search.find_track\nmatch_phrase title + artist]
    C --> D{exact case-insensitive guard passes?}
    D -- yes --> E{synced_lyrics present?}
    E -- yes --> Z[Return cached Lyrics]
    E -- no --> F[Fetch LRCLIB]
    D -- no --> F
    B -- no --> F
    F --> G{synced lyrics found?}
    G -- yes --> H[Write-through cache\nsource=lrclib-cache]
    G -- no --> I{local audio and transcribe?}
    I -- yes --> J[faster-whisper -> LRC]
    J --> H
    I -- no --> K[Return plain/no lyrics]
    H --> Z
```

## Live sync modes

| Mode | Position source | Re-lock behavior | Notes |
| --- | --- | --- | --- |
| File/text | User presses Enter at music start | None | Optional `--offset`; file mode can detect real beats. |
| Spotify | Spotify Web API `progress_ms` | Polls current playback; moves to next track automatically. | Does not download audio; lyrics are LRCLIB/cache only. |
| Listen/output | songrec match `offset` + `time.monotonic()` anchor | One-shot lock | `--lead` applies a default forward bias for recognition latency. |
| Radio | Repeated songrec matches | Re-anchors same track, swaps timeline on new track | Keeps rendering while speech/ad/quiet sections do not match. |
| YouTube | User presses Enter at music start | None | yt-dlp title → smart parse → LRCLIB; `--download` unlocks Whisper/beats and auto-prunes downloaded audio to `KARAOKE_YT_CACHE_MAX_MB`; `--cookies-from-browser`/`--cookies` authenticate for Premium-quality/library access. Live-position sync not available — use `--output`/`--radio` for that. |

## Ingest and semantic search flow

```mermaid
flowchart TD
    A[music-index --dir ~/Music] --> B[iter_audio_files]
    B --> C[mutagen extract_tags]
    C --> D[LRCLIB fetch]
    D --> E[plain lyrics + synced LRC]
    C --> F[metadata fallback text]
    E --> G[embedding source\nlyrics if present else metadata]
    F --> G
    G --> H[sentence-transformers\nnormalized 384-d vector]
    H --> I[(OpenSearch document)]
    I --> J[lyricsearch query]
    J --> K[embed query]
    K --> L[kNN over lyrics_vector]
```

## Deployment boundary

OpenSearch runs locally on a kind cluster and is exposed to the host at `http://localhost:9200` by default. The CLI stays on the host, which keeps audio devices, Spotify credentials and terminal rendering outside Kubernetes.

```mermaid
flowchart TB
    subgraph Host[Linux host]
        CLI[karaoke / lyricsearch / music-index]
        Auth[~/.hermes/auth.json\nSpotify refresh token]
        Audio[PipeWire/Pulse sources\nmic + monitor]
    end

    subgraph Kind[kind-karaoke cluster]
        OS[(OpenSearch single node\nDISABLE_SECURITY_PLUGIN=true\nlocal dev only)]
    end

    CLI -->|localhost:9200| OS
    CLI --> Auth
    CLI --> Audio
```
