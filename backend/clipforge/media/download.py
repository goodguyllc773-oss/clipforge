"""Import-by-URL: fetch a video the user pointed at by pasting a link.

A deliberate, scoped exception to "no outbound network calls" (see
``docs/decisions.md`` D13) — this module is reached only when the user pastes a
URL into the import dialog and submits it. Nothing here polls, lists or crawls a
source on its own; each call fetches exactly the one link it was given, once.

Two paths, chosen by :func:`classify_url`:

* **direct** — the URL already points at a video file (its path ends in one of
  ``media.probe.SUPPORTED_EXTENSIONS``): a plain streaming HTTP download.
* **ytdlp** — anything else (YouTube, TikTok, a page that embeds a player, a CDN
  link with no recognisable extension): resolved and fetched with ``yt-dlp``,
  restricted to the single URL given (``noplaylist=True``).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from ..errors import DependencyMissing, DownloadFailed, InvalidUrl, JobCancelled, UnsupportedSource
from .paths import safe_filename
from .probe import SUPPORTED_EXTENSIONS

USER_AGENT = "Mozilla/5.0 (compatible; ClipForge/1.0; +local-first video clipper)"
CHUNK = 1 << 20  # 1 MiB
CONNECT_TIMEOUT_S = 30
_ALLOWED_SCHEMES = {"http", "https"}

# fraction is None when the source doesn't expose a total size (common for
# fragmented/HLS formats) — callers still get `done_bytes` so they can show
# live progress instead of an apparently frozen bar.
ProgressFn = Callable[[float | None, int], None]
CancelFn = Callable[[], None]  # raises JobCancelled if cancelled


def validate_url(raw: str) -> str:
    """Normalize and sanity-check a user-pasted URL. Raises ``InvalidUrl``."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidUrl("URL is empty.")
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidUrl(f"Only http/https links are supported, got: {url}",
                         hint="Paste a direct video link or a page URL (e.g. a YouTube link).")
    if not parsed.netloc:
        raise InvalidUrl(f"That doesn't look like a URL: {url}")
    return url


def classify_url(url: str) -> str:
    """"direct" if the path ends in a known video extension, else "ytdlp"."""
    path = unquote(urlparse(url).path)
    ext = Path(path).suffix.lower()
    return "direct" if ext in SUPPORTED_EXTENSIONS else "ytdlp"


def _filename_from_url(url: str, fallback: str = "download") -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name or fallback
    return safe_filename(name, fallback=fallback)


def download_direct(url: str, dest_dir: Path, *, on_progress: ProgressFn | None = None,
                    cancel_check: CancelFn | None = None) -> Path:
    """Stream ``url`` straight to disk. Used when the URL already points at a
    supported video file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _filename_from_url(url, fallback="video.mp4")
    final = dest_dir / name
    part = final.with_name(final.name + ".part")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=CONNECT_TIMEOUT_S) as resp:
            total = resp.headers.get("Content-Length")
            total_bytes = int(total) if total and total.isdigit() else None
            done = 0
            with part.open("wb") as f:
                while True:
                    if cancel_check:
                        cancel_check()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        frac = min(1.0, done / total_bytes) if total_bytes else None
                        on_progress(frac, done)
    except JobCancelled:
        part.unlink(missing_ok=True)
        raise
    except HTTPError as e:
        part.unlink(missing_ok=True)
        raise DownloadFailed(f"The server returned {e.code} for this link.", retryable=e.code >= 500,
                             hint="Check the link still works in a browser." if e.code in (403, 404)
                             else None) from e
    except URLError as e:
        part.unlink(missing_ok=True)
        raise DownloadFailed(f"Couldn't reach that link: {e.reason}.", retryable=True,
                             hint="Check your network connection and that the link is still up.") from e
    except OSError as e:
        part.unlink(missing_ok=True)
        raise DownloadFailed(f"Couldn't save the download: {e.strerror or e}.", retryable=True) from e
    if final.exists():
        final.unlink()
    part.rename(final)
    if final.stat().st_size == 0:
        final.unlink(missing_ok=True)
        raise DownloadFailed("The download was empty.", retryable=True)
    return final


_UNSUPPORTED_RE = re.compile(r"unsupported url", re.IGNORECASE)


def download_via_ytdlp(url: str, dest_dir: Path, *, on_progress: ProgressFn | None = None,
                       cancel_check: CancelFn | None = None) -> Path:
    """Resolve and fetch ``url`` with yt-dlp (YouTube, TikTok, and most other
    video sites). Restricted to the single video the URL points at."""
    try:
        import yt_dlp
    except ImportError as e:
        raise DependencyMissing(
            "yt-dlp isn't installed.", hint="Install backend dependencies again "
            "(setup.cmd / scripts/setup-mac.sh) — yt-dlp is part of the backend package.") from e

    dest_dir.mkdir(parents=True, exist_ok=True)
    cancelled = {"flag": False}

    def hook(d: dict) -> None:
        if cancel_check:
            try:
                cancel_check()
            except JobCancelled:
                cancelled["flag"] = True
                raise
        if on_progress and d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            # Deliberately not `total_bytes_estimate`: for fragmented/HLS
            # formats yt-dlp recomputes it from the *current* fragment's size
            # each call, so while one large fragment is mid-download the
            # estimate — and the ratio derived from it — stays frozen even as
            # real bytes keep flowing. A real `total_bytes` (e.g. a plain
            # progressive download) is trustworthy; the estimate isn't.
            total = d.get("total_bytes")
            on_progress(min(1.0, done / total) if total else None, done)

    opts = {
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,  # the pasted link, and only that link — no channel/playlist crawling
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [hook],
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except JobCancelled:
        raise
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        if cancelled["flag"]:
            raise JobCancelled() from e
        message = str(e)
        if _UNSUPPORTED_RE.search(message):
            raise UnsupportedSource(
                f"That link isn't from a supported site: {url}",
                hint="Paste a direct video file link, or a page from a site yt-dlp supports "
                     "(YouTube, TikTok, and most others).") from e
        raise DownloadFailed(f"Couldn't download that video: {message.splitlines()[-1]}", retryable=True) from e

    candidates = sorted(
        (p for p in dest_dir.glob("source.*") if p.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda p: p.stat().st_size, reverse=True)
    if not candidates:
        raise DownloadFailed("yt-dlp finished but no playable video file was produced.", retryable=True)
    return candidates[0]


def fetch(url: str, dest_dir: Path, *, on_progress: ProgressFn | None = None,
         cancel_check: CancelFn | None = None) -> tuple[Path, str]:
    """Download ``url`` into ``dest_dir``. Returns ``(path, kind)`` where ``kind``
    is "direct" or "ytdlp"."""
    kind = classify_url(url)
    if kind == "direct":
        return download_direct(url, dest_dir, on_progress=on_progress, cancel_check=cancel_check), kind
    return download_via_ytdlp(url, dest_dir, on_progress=on_progress, cancel_check=cancel_check), kind
