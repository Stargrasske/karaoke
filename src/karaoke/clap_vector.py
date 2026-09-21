"""Audio embeddings that share a space with text, via CLAP.

The hand-built vector in :mod:`karaoke.audio_vector` describes a track's
spectral shape, and measurement showed the limit of that: across 3000 random
pairs its cosine similarity ran a median of 0.965 with a p5-p95 spread of 0.10,
and mean-centring widened the spread fourteenfold *without reordering the
neighbours*. The narrow range was cosmetic; the features themselves put The
Cranberries next to Macy Gray.

CLAP is trained on audio paired with text, which buys two things that vector
cannot offer at any amount of tuning:

- **Neighbours that hold up.** Median similarity 0.779 with p5-p95 spanning
  0.59-0.91 — a real spread, arrived at without any centring trick.
- **Text queries over audio.** "heavy distorted guitar rock" returns Mastodon
  and Dinosaur Jr.; "electronic dance beat" returns Modjo and Boy Harsher.
  From a library the model has never seen, with no lyrics, tags or metadata
  involved. That matters most for instrumentals, which have no words to embed
  and were previously unreachable by any query at all.

No new package: torch and transformers are already installed. The weights are a
~600 MB download, cached by huggingface after the first use.

This does not replace the 62-dimension vector. That one is cheap, needs no
model, and stays useful for near-duplicate detection between two captures of
the same performance; this one is for "what does it sound like".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .logger import log

MODEL_NAME = "laion/clap-htsat-unfused"
CLAP_INDEX = "karaoke-clap"
CLAP_DIM = 512

# CLAP is trained at 48kHz, unlike the 22.05kHz the spectral vector uses: its
# front end expects that rate and resampling to anything else changes what the
# model hears.
SAMPLE_RATE = 48000

# Ten-second windows, spread across the track rather than taken from its
# opening. A song's first ten seconds are regularly an intro that sounds
# nothing like the rest of it, which is the same trap that made Whisper detect
# the wrong language.
WINDOW_SECONDS = 10
MAX_WINDOWS = 6

# Sample the body of the track, not its edges. Spreading windows across the
# whole file still puts one of them at sample 0, and measured against real
# tracks that opening window is often a different sound entirely -- cosine
# 0.55 and 0.62 against the rest of the song on two of four sampled files,
# which is a fade-in or a count-in rather than the music. An outro fade is the
# same problem at the other end. Both would otherwise carry a full 1/6 of the
# pooled vector.
#
# Trimmed as a fraction rather than a fixed number of seconds because intros
# scale with the track: a 90-second punk song does not get a 20-second intro.
EDGE_TRIM = 0.06

# Below this a track has no body worth trimming -- an interlude or a jingle is
# mostly edges -- so it is embedded whole rather than cut down to nothing.
MIN_TRIM_SECONDS = 45

# What similarity means here, measured over 60 library tracks:
#     p5 +0.589   median +0.779   p95 +0.909
# Well spread compared with the spectral vector's 0.885-0.987, so a score can
# be read directly rather than needing percentile translation.
SIMILARITY_TYPICAL = 0.779
SIMILARITY_NOTABLE = 0.909

_model = None
_processor = None


def available() -> bool:
    """Whether the CLAP stack can be imported at all."""
    try:
        import torch  # noqa: F401
        from transformers import ClapModel, ClapProcessor  # noqa: F401
    except Exception:
        return False
    return True


def _silence_progress_bars() -> None:
    """Stop transformers drawing a tqdm bar while loading weights.

    Not cosmetic. tqdm builds a `multiprocessing.RLock` for its write lock,
    and inside a Textual worker thread that reaches the multiprocessing
    resource tracker and fails -- so the progress bar took the model load down
    with it, and every `k` sample logged "CLAP audio embedding failed" while
    the same call worked perfectly from a shell.

    A TUI has nowhere to draw a progress bar anyway.
    """
    import os
    import threading

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        from tqdm import tqdm

        tqdm.set_lock(threading.RLock())
    except Exception:
        pass
    try:
        from transformers.utils import logging as hf_logging

        hf_logging.disable_progress_bar()
    except Exception:
        log.debug("could not disable transformers progress bars", exc_info=True)


def _load():
    """Load and cache the model. Slow once, then instant."""
    global _model, _processor
    if _model is not None:
        return _model, _processor
    _silence_progress_bars()
    from transformers import ClapModel, ClapProcessor

    _processor = ClapProcessor.from_pretrained(MODEL_NAME)
    _model = ClapModel.from_pretrained(MODEL_NAME).eval()
    return _model, _processor


def _unwrap(out):
    """transformers 5.x returns a model output object, not a tensor."""
    return out if hasattr(out, "shape") else out.pooler_output


def _normalise(vector) -> list[float]:
    import numpy as np

    v = np.asarray(vector, dtype=float)
    norm = float(np.linalg.norm(v))
    return [float(x) for x in (v / norm if norm else v)]


def _body_windows(y, max_windows: int = MAX_WINDOWS, *, full: bool = False) -> list:
    """Even windows across the track's body, skipping intro and outro.

    Separated out so the windowing can be tested and compared without loading
    a model: what a vector means depends entirely on which audio went into it,
    so two vectors built under different windowing are not comparable and the
    whole index has to be rebuilt when this changes.

    ``full`` tiles the body end to end instead of sampling it, so nothing is
    skipped. Six ten-second windows cover about a quarter of a four-minute
    track, which is ample for timbre -- the thing CLAP describes -- but a
    caller wanting every second can ask. Cost scales with the window count,
    since each one is a forward pass.
    """
    total = len(y)
    span = SAMPLE_RATE * WINDOW_SECONDS

    start, end = 0, total
    if total >= SAMPLE_RATE * MIN_TRIM_SECONDS:
        trim = int(total * EDGE_TRIM)
        start, end = trim, total - trim

    body = y[start:end]
    if len(body) < span:
        # Trimming left less than one window; the edges are all there is.
        body = y

    if full:
        windows = [body[i:i + span] for i in range(0, len(body), span)]
    else:
        count = max(1, max_windows)
        step = max(1, len(body) // count)
        windows = [body[i:i + span] for i in range(0, len(body), step)][:count]
    return [w for w in windows if len(w) >= SAMPLE_RATE]


def embed_audio(audio_path: str, *, max_windows: int = MAX_WINDOWS,
                full: bool = False) -> Optional[list[float]]:
    """Embed a track, or None if it cannot be read.

    Windows are mean-pooled: a single ten-second slice describes a moment
    rather than a song, and a track that changes character would be
    represented by whichever part happened to be sampled.

    ``max_windows`` and ``full`` control how much of the track is sampled.
    Note that changing either produces vectors that are not comparable with
    those already indexed, so it is a decision for a whole rebuild rather than
    one track.
    """
    if not available():
        log.debug("CLAP unavailable; no audio embedding")
        return None
    try:
        import librosa
        import numpy as np
        import torch

        from .analyze import _as_wav

        model, processor = _load()
        # _as_wav first: libsndfile cannot open the webm/opus the cache holds,
        # which every other audio path here goes through ffmpeg for.
        with _as_wav(audio_path, SAMPLE_RATE) as wav:
            if not wav:
                return None
            y, _sr = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
        if y is None or len(y) < SAMPLE_RATE:
            return None

        windows = _body_windows(y, max_windows, full=full)
        if not windows:
            return None

        with torch.no_grad():
            inputs = processor(audio=windows, sampling_rate=SAMPLE_RATE,
                               return_tensors="pt")
            feats = _unwrap(model.get_audio_features(**inputs)).numpy()
        pooled = np.asarray(feats).mean(axis=0)
    except Exception:
        log.debug("CLAP audio embedding failed for %s", audio_path, exc_info=True)
        return None

    if len(pooled) != CLAP_DIM:
        log.warning("CLAP vector has %d dims, expected %d; discarding",
                    len(pooled), CLAP_DIM)
        return None
    return _normalise(pooled)


def embed_text(query: str) -> Optional[list[float]]:
    """Embed a description, into the same space as the audio."""
    if not available() or not (query or "").strip():
        return None
    try:
        import torch

        model, processor = _load()
        with torch.no_grad():
            inputs = processor(text=[query], return_tensors="pt", padding=True)
            vector = _unwrap(model.get_text_features(**inputs)).numpy()[0]
    except Exception:
        log.debug("CLAP text embedding failed for %r", query, exc_info=True)
        return None
    return _normalise(vector)


def ensure_index(os_client: Any, index_name: str = CLAP_INDEX) -> bool:
    """Create the CLAP index if absent. True if it was created."""
    if os_client.indices.exists(index=index_name):
        return False
    os_client.indices.create(index=index_name, body={
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "track_id": {"type": "integer"},
                "artist": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "album": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "source": {"type": "keyword"},
                "detected_key": {"type": "keyword"},
                "bpm": {"type": "float"},
                "embedded_at": {"type": "date"},
                # Declared rather than left to dynamic mapping, which types a
                # string as `text` with a `.keyword` subfield -- so an
                # aggregation on `genre` fails while one on `genre.keyword`
                # works, and which of those a caller must use depends on
                # whether the index was created before or after the field
                # existed. Declaring it removes that difference.
                "genre": {"type": "keyword"},
                "genre_score": {"type": "float"},
                "genre_runner_up": {"type": "keyword"},
                "clap_vector": {
                    "type": "knn_vector",
                    "dimension": CLAP_DIM,
                    "method": {"name": "hnsw", "space_type": "cosinesimil",
                               "engine": "lucene"},
                },
            }
        },
    })
    return True


def doc_id(track_id: int) -> str:
    """One embedding per track.

    Unlike the spectral index, which keys recordings by start time because two
    captures of a song are two observations, this describes the music rather
    than a performance -- so re-running replaces.
    """
    return f"clap:{track_id}"


def store(track_id: int, vector: list[float], *, artist: str = "", title: str = "",
          album: str = "", detected_key: str = "", bpm: Optional[float] = None,
          os_client: Any = None) -> bool:
    """Index an embedding so it becomes searchable. Returns success.

    Embedding is the expensive step -- decoding audio and running CLAP --
    while indexing is a single small write. Both ingest paths used to compute
    a vector, read a genre word off it and drop it, which is why a
    whole-library pass produced ~13k genre labels and 246 searchable vectors.
    Call this wherever a vector is produced.

    Best-effort: a caller is generally part-way through a pipeline whose other
    results (key, tempo, genre) must survive OpenSearch being unavailable, so
    failure is logged and reported rather than raised.
    """
    from datetime import datetime, timezone

    if not vector:
        return False
    try:
        client = os_client
        if client is None:
            from .osclient import client as get_os_client

            client = get_os_client()
        if client is None:
            return False
        ensure_index(client)
        client.index(
            index=CLAP_INDEX,
            id=doc_id(track_id),
            body=build_doc(
                track_id=track_id, artist=artist, title=title, album=album,
                vector=vector, detected_key=detected_key, bpm=bpm,
                embedded_at=datetime.now(timezone.utc).isoformat(),
            ),
        )
        return True
    except Exception:
        log.debug("could not index CLAP vector for track %s", track_id, exc_info=True)
        return False


def build_doc(*, track_id: int, artist: str, title: str, vector: list[float],
              embedded_at: str, album: str = "", source: str = "library",
              detected_key: str = "", bpm: Optional[float] = None) -> dict:
    """Assemble the document for one embedded track."""
    return {
        "track_id": track_id,
        "artist": artist,
        "title": title,
        "album": album,
        "source": source,
        "detected_key": detected_key,
        "bpm": bpm,
        "embedded_at": embedded_at,
        "clap_vector": vector,
    }
