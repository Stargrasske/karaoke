# API reference

This reference is generated from Python docstrings with `mkdocstrings`.

## CORS (web dashboard)

The library API binds `0.0.0.0` for cluster deployment and now sends CORS
headers so a browser-based frontend (e.g. the Angular app at `karaoke/web/`)
can call it cross-origin. Allowed origins default to `http://localhost:4200`
and are configurable via the comma-separated `KARAOKE_WEB_ORIGIN` environment
variable.

## Background jobs

`POST /api/staging/youtube` and `POST /api/staging/whisper` return a `job_id`
alongside `status: "accepted"`; poll `GET /api/jobs/{job_id}` for
`pending`/`running`/`done`/`error`. See [Control API](control-api.md#jobs-background-task-status)
for the equivalent on the host-side control API (recording analysis, folder scans).

::: karaoke.jobs

## CLI

::: karaoke.cli

## Configuration

::: karaoke.config

## Song identification

::: karaoke.identify

## Lyrics and LRC parsing

::: karaoke.lyrics

## Timeline and terminal player

::: karaoke.player

## Search

::: karaoke.search

## Local cache and stats

::: karaoke.localcache

## Scanner and document building

::: karaoke.scanner

## OpenSearch client and index mapping

::: karaoke.osclient

## Embeddings

::: karaoke.embed

## Audio tags

::: karaoke.tags

## Spotify client

::: karaoke.spotify_client

## Spotify import

::: karaoke.spotify_import

## Whisper transcription

::: karaoke.whisper_sync

## Mood tinting

::: karaoke.sentiment

## Beat flash

::: karaoke.beats


## Admin & Operator TUI

::: karaoke.admin_tui

## Audio Key & Tempo Analysis

::: karaoke.analyze

## FastAPI Library REST API

::: karaoke.api

## FastAPI Client Library

::: karaoke.api_client

## Audio Feature Vector Embeddings

::: karaoke.audio_vector

## Automated Track Classification

::: karaoke.autoclassify

## Collection Metadata Backfill

::: karaoke.backfill

## Backfill Queue Runner

::: karaoke.backfill_runner

## Large Terminal Text Rendering

::: karaoke.bigtext

## Interactive Library Browser TUI

::: karaoke.browse

## Closed Caption & Subtitle Synchronization

::: karaoke.caption_sync

## Celery Task Queue & Orchestration

::: karaoke.celery_app

## CLAP Audio Genre Vectors

::: karaoke.clap_vector

## Artwork Storage & Caching

::: karaoke.cover_store

## Album Artwork Fetching & Embedding

::: karaoke.coverart

## Host Playback Control API

::: karaoke.ctrl_api

## Audio Fingerprinting & Recognition

::: karaoke.detect

## Event Hooks & Pub/Sub System

::: karaoke.events

## Streaming & Local Source Resolution

::: karaoke.find_sources

## Collection & Directory Scanning

::: karaoke.folder_scan

## Genre Tagging & Classification

::: karaoke.genre

## OpenSearch & SQLite Search Engine

::: karaoke.librarysearch

## Library & Playback Statistics

::: karaoke.librarystats

## Process Synchronization & File Locking

::: karaoke.lockfile

## Structured Logging & Diagnostics

::: karaoke.logger

## Lyric Alignment & Timings

::: karaoke.lyric_align

## Language Detection for Lyrics

::: karaoke.lyric_language

## Mood-Based Visual Artwork

::: karaoke.moodart

## Mood Video & Animation Framing

::: karaoke.moodframe

## Free-Text Mood and Vibe Matching

::: karaoke.mood_match

## Musical Scales, Keys, and Theory Helpers

::: karaoke.musictheory

## Follow-Mode Playback Controller

::: karaoke.player_follow

## Browser Kiosk & Media Player Launcher

::: karaoke.player_open

## Synced Playback State Coordination

::: karaoke.player_sync

## MPRIS / Media Controller Client

::: karaoke.playerctl

## Post-Processing Task Queue

::: karaoke.postprocess_queue

## Worker & Pipeline Status Reporting

::: karaoke.postprocess_status

## Background Post-Processing Worker

::: karaoke.postprocess_worker

## Smart Queue & Recommendation Engine

::: karaoke.queue_suggest

## Radio Session Recording & Library Import

::: karaoke.radio_pipeline

## Live Audio Capture & PipeWire Recording

::: karaoke.recorder

## Audio Segment Processing for Recordings

::: karaoke.recording_audio

## FLAC Slicing & Export for Recordings

::: karaoke.recording_slice

## Recording Cut & Transcode Worker

::: karaoke.recording_worker

## Audio Sampling & Quick Key/BPM Inspection

::: karaoke.sample_audio

## Silence Trimming & Audio Preprocessing

::: karaoke.silence

## Smart Playlists & Query Filters

::: karaoke.smartlist

## Source Priority & Selection Rules

::: karaoke.source_select

## Spotify Playlist Sync & Management

::: karaoke.spotify_playlist

## Staged Downloads & Pre-Ingestion

::: karaoke.stage_sources

## Staging Directory & File State

::: karaoke.staging

## Staging Control API

::: karaoke.staging_api

## Celery / Background Tasks Definition

::: karaoke.tasks

## Tone Analysis & Mood Scoring

::: karaoke.tone

## Multi-Modal Track Analysis Coordinator

::: karaoke.track_analysis

## Karaoke Terminal User Interface

::: karaoke.tui

## LRC Word-Timing Upgrade Engine

::: karaoke.upgrade_timings

## OpenSearch Vector Indexing & KNN Search

::: karaoke.vector_index

## Rich Terminal Visualizations & Meters

::: karaoke.visuals

## Web Interface & HTML Endpoints

::: karaoke.web

## Whisper Transcript Post-Processing

::: karaoke.whisper_clean

## YouTube Playback & Cache Resolution

::: karaoke.youtube

## YouTube Music API Client

::: karaoke.ytmusic_client

## YouTube Music Synced Lyrics Fetcher

::: karaoke.ytmusic_lyrics

## YouTube Music Playlist Synchronization

::: karaoke.ytmusic_playlist


## Artist Classifier

::: karaoke.artist_classifier

## Stage View

::: karaoke.stage_view


## Cache Ingest

::: karaoke.cache_ingest


## Event Store

::: karaoke.event_store

## Relay

::: karaoke.relay


## Dj Chat

::: karaoke.dj_chat

## Mcp Server

::: karaoke.mcp_server


## Chords

::: karaoke.chords

## Key Progression

::: karaoke.key_progression

## `GET /api/workers/status`

Celery worker, queue and Flower dashboard status. Allows monitoring the
post-processing pipeline without screen-scraping the TUI.

```json
{
  "orchestrator": "celery",
  "available": true,
  "dashboard_url": "http://127.0.0.1:5555",
  "workers_active": 1,
  "queue_depth": 0,
  "queue_details": {
    "name": "karaoke-postprocess-celery",
    "messages": 0,
    "messages_ready": 0,
    "messages_unacknowledged": 0,
    "consumers": 1
  },
  "worker_details": [
    {
      "unit": "karaoke-celery-worker.service",
      "active": "active",
      "running": true
    }
  ]
}
```

The `orchestrator` field indicates the active backend (`celery` or `legacy`).
`available: false` means RabbitMQ is unreachable or no workers are running; a
`reason` field is also present on failure. CPU and memory metrics are no
longer exposed via this API directly.

### `POST /api/workers/scale`

Scales the Celery worker pool (starts/stops `karaoke-celery-worker.service`).

Request body:

```json
{
  "target": 1
}
```

`target` (integer): Desired number of active workers. `0` stops the worker;
`1` (or higher) starts it. Returns `200 OK` on success.
