"""Static configuration.

Values come from environment variables (``CLIPFORGE_*``) with sensible per-OS
defaults. User-editable preferences (Whisper model, encoder preference, default
export folder) live in the ``app_settings`` table instead — see ``services.py``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ClipForge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ClipForge"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "clipforge"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    # Which transcription engine the worker uses: "faster-whisper" or "sidecar".
    transcriber: str = "faster-whisper"
    # Run worker threads inside the API process.
    embedded_worker: bool = True
    # Seconds of audio per transcription chunk (bounded RAM + resume granularity).
    transcribe_chunk_s: float = 600.0
    lease_s: float = 30.0
    heartbeat_s: float = 5.0
    poll_s: float = 1.0
    frontend_dist: Path | None = None
    extra_allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    # Publishing: OAuth client credentials for each platform. Set once per
    # machine after the user registers their own app (Google Cloud Console /
    # TikTok for Developers) — see README "Connecting YouTube/TikTok". Never
    # committed; these are deployment config, not user data, so they live here
    # rather than in the app_settings table.
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    # Base URL the OAuth callback is registered under with each platform.
    oauth_redirect_base: str = "http://127.0.0.1:8765"
    # TikTok rejects localhost/127.0.0.1 redirect URIs outright (unlike
    # Google, which supports loopback redirects natively) — local testing
    # needs a real HTTPS URL in front of this server (e.g. an ngrok tunnel,
    # README "Connecting TikTok"). Overrides oauth_redirect_base for TikTok
    # only, so YouTube's already-registered 127.0.0.1 redirect keeps working.
    tiktok_redirect_base: str = ""

    def redirect_base(self, platform: str) -> str:
        if platform == "tiktok" and self.tiktok_redirect_base:
            return self.tiktok_redirect_base
        return self.oauth_redirect_base

    # ---- data-dir layout -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipforge.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def downloads_dir(self) -> Path:
        """Import-by-URL: fetched files land here and stay (they become the
        media's ``original_path``, same as a locally-browsed file)."""
        return self.data_dir / "downloads"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "worker.lock"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.logs_dir, self.models_dir, self.cache_dir, self.renders_dir,
                  self.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Load a `.env` file from the repo root, if present — mainly for the
    publishing OAuth client credentials (README "Connecting YouTube/TikTok"),
    so they don't have to go through Windows' system environment variables.
    Never overrides a variable the environment already set. Safe to call
    repeatedly; does nothing after the first call."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env", override=False)


def load_settings(**overrides) -> Settings:
    _load_dotenv_once()
    env = os.environ
    repo_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    values = dict(
        data_dir=Path(env.get("CLIPFORGE_DATA_DIR") or _default_data_dir()),
        host=env.get("CLIPFORGE_HOST", "127.0.0.1"),
        port=int(env.get("CLIPFORGE_PORT", "8765")),
        ffmpeg=env.get("CLIPFORGE_FFMPEG", "ffmpeg"),
        ffprobe=env.get("CLIPFORGE_FFPROBE", "ffprobe"),
        transcriber=env.get("CLIPFORGE_TRANSCRIBER", "faster-whisper"),
        embedded_worker=_bool(env.get("CLIPFORGE_EMBEDDED_WORKER"), True),
        transcribe_chunk_s=float(env.get("CLIPFORGE_TRANSCRIBE_CHUNK_S", "600")),
        frontend_dist=Path(env["CLIPFORGE_FRONTEND_DIST"]) if env.get("CLIPFORGE_FRONTEND_DIST")
        else (repo_dist if repo_dist.is_dir() else None),
        youtube_client_id=env.get("CLIPFORGE_YOUTUBE_CLIENT_ID", ""),
        youtube_client_secret=env.get("CLIPFORGE_YOUTUBE_CLIENT_SECRET", ""),
        tiktok_client_key=env.get("CLIPFORGE_TIKTOK_CLIENT_KEY", ""),
        tiktok_client_secret=env.get("CLIPFORGE_TIKTOK_CLIENT_SECRET", ""),
        oauth_redirect_base=env.get("CLIPFORGE_OAUTH_REDIRECT_BASE", "http://127.0.0.1:8765"),
        tiktok_redirect_base=env.get("CLIPFORGE_TIKTOK_REDIRECT_BASE", ""),
        extra_allowed_origins=tuple(
            h.strip() for h in env.get("CLIPFORGE_EXTRA_ALLOWED_ORIGINS", "").split(",") if h.strip()),
    )
    values.update(overrides)
    if not isinstance(values["data_dir"], Path):
        values["data_dir"] = Path(values["data_dir"])
    return Settings(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
