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


def embed_audio(audio_path: str) -> Optional[list[float]]:
    """Embed a track, or None if it cannot be read.

    Windows are mean-pooled: a single ten-second slice describes a moment
    rather than a song, and a track that changes character would be
    represented by whichever part happened to be sampled.
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

        step = max(1, len(y) // MAX_WINDOWS)
        windows = [y[i:i + SAMPLE_RATE * WINDOW_SECONDS]
                   for i in range(0, len(y), step)][:MAX_WINDOWS]
        windows = [w for w in windows if len(w) >= SAMPLE_RATE]
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
