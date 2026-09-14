"""Lyric/text embeddings via sentence-transformers.

The model is loaded lazily (and cached as a module singleton) so importing this
module is cheap and CLI startup stays fast until an embedding is actually needed.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from .config import settings

_model: Any = None


def _get_model() -> Any:
    global _model
    if _model is None:
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
            pass

        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(settings.embed_model)
    return _model


def embed_text(text: str) -> list[float]:
    """Return a normalized embedding vector for a single string."""
    model = _get_model()
    vec = model.encode(text or "", normalize_embeddings=True)
    return [float(x) for x in vec]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings in one batched call."""
    if not texts:
        return []
    model = _get_model()
    vecs = model.encode(
        [t or "" for t in texts], normalize_embeddings=True, batch_size=32
    )
    return [[float(x) for x in v] for v in vecs]


@lru_cache(maxsize=1)
def embed_dim() -> int:
    """Actual embedding dimension of the configured model."""
    return len(embed_text("dimension probe"))
