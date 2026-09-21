"""Environment-driven settings for karaoke.

All configuration comes from environment variables (optionally loaded from a
.env file at the project root) so the same code runs on the host CLI and inside
the in-cluster scanner Job.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no external dep). Does not override real env vars."""
    candidates = [Path.cwd() / ".env"]
    # Project root relative to src/karaoke/config.py, when running from a source
    # checkout. Guard the index: in an installed/containerised layout the
    # package is not necessarily three levels below a project root.
    parents = Path(__file__).resolve().parents
    if len(parents) > 3:
        candidates.append(parents[3] / ".env")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
        break


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration for CLI, scanner and cache clients.

    Values are loaded once at import time from the environment, with optional
    project-local `.env` support. Defaults target a localhost OpenSearch service
    exposed by the local `kind-karaoke` cluster.
    """

    opensearch_url: str
    music_dir: Path
    embed_model: str
    index_name: str
    embed_dim: int
    lrclib_base: str
    kube_context: str
    data_dir: Path
    yt_cache_max_mb: int
    db_backend: str
    db_url: str

    @property
    def local_db(self) -> Path:
        """Path to the cluster-independent local SQLite cache/stats database."""
        return self.data_dir / "karaoke.db"

    @property
    def uses_postgres(self) -> bool:
        """True when configured to use a Postgres backend for bigger collections.

        Postgres support is a planned backend for large libraries; SQLite (WAL,
        indexed) remains the default and the only fully-wired backend today.
        See docs/database.md for the migration roadmap.
        """
        return self.db_backend.strip().lower() in ("postgres", "postgresql", "pg")

    @property
    def youtube_dir(self) -> Path:
        """Directory holding downloaded YouTube audio (``--youtube --download``)."""
        return self.data_dir / "youtube"

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from `.env` plus process environment variables."""
        _load_dotenv()
        default_data = Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        ) / "karaoke"
        return cls(
            opensearch_url=os.environ.get("OPENSEARCH_URL", "http://localhost:9200"),
            music_dir=Path(os.environ.get("MUSIC_DIR", str(Path.home() / "Music"))).expanduser(),
            embed_model=os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2"),
            index_name=os.environ.get("KARAOKE_INDEX", "tracks"),
            embed_dim=int(os.environ.get("EMBED_DIM", "384")),
            lrclib_base=os.environ.get("LRCLIB_BASE", "https://lrclib.net"),
            kube_context=os.environ.get("KUBE_CONTEXT", "kind-karaoke"),
            data_dir=Path(os.environ.get("KARAOKE_DATA_DIR", str(default_data))).expanduser(),
            yt_cache_max_mb=int(os.environ.get("KARAOKE_YT_CACHE_MAX_MB", "500")),
            # VESTIGIAL: the backend is no longer selectable. localcache cut
            # over to Postgres unconditionally and reads KARAOKE_PG_URL
            # directly; nothing in the connection path consults these two
            # settings. Kept only so existing imports of settings.db_backend /
            # settings.db_url keep resolving — see docs/database.md.
            db_backend=os.environ.get("KARAOKE_DB_BACKEND", "sqlite"),
            db_url=os.environ.get("KARAOKE_DB_URL", ""),
        )


settings = Settings.load()
