"""Provider-neutral publishing: one adapter per platform behind a common
interface (``base.py``), tokens in the OS keychain (``credentials.py``), never
the database. See ``docs/decisions.md`` D14 and ``docs/architecture.md``
"Publishing"."""

from __future__ import annotations

from .base import PlatformAdapter
from .tiktok import TikTokAdapter
from .youtube import YouTubeAdapter

ADAPTERS: dict[str, PlatformAdapter] = {
    "youtube": YouTubeAdapter(),
    "tiktok": TikTokAdapter(),
}


def adapter(platform: str) -> PlatformAdapter:
    try:
        return ADAPTERS[platform]
    except KeyError:
        from ..errors import ValidationFailed
        raise ValidationFailed(f"Unknown publishing platform '{platform}'.",
                               hint=f"Supported: {', '.join(ADAPTERS)}.")
