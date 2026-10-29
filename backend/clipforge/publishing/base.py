"""The interface every platform adapter implements. The publish job handler
(``jobs/handlers.py::handle_publish``) only ever talks to this — it never
imports a platform SDK directly, so adding a platform never touches the
generic publish pipeline or the job queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from ..config import Settings

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], None]


@dataclass
class AccountInfo:
    external_account_id: str
    display_name: str
    thumbnail_url: str | None = None
    scopes: list[str] = field(default_factory=list)


@dataclass
class PublishRequest:
    file_path: Path
    title: str
    description: str
    tags: list[str]
    privacy: str  # private | unlisted | public
    scheduled_for: datetime | None = None


@dataclass
class PublishResult:
    # uploaded | draft | scheduled | published — the adapter's own honest read
    # of what the platform actually confirmed, never upgraded by the caller.
    status: str
    platform_upload_id: str | None = None
    platform_post_id: str | None = None
    platform_post_url: str | None = None
    note: str = ""


class PlatformAdapter(Protocol):
    name: str

    def authorize_url(self, settings: "Settings", redirect_uri: str, state: str) -> str:
        """The platform's OAuth consent URL the user is sent to."""
        ...

    def exchange_code(self, settings: "Settings", redirect_uri: str, code: str) -> tuple[dict[str, Any], AccountInfo]:
        """Trade the OAuth callback's ``code`` for a token (stored verbatim in
        the keychain) and the account identity to show the user."""
        ...

    def refresh(self, settings: "Settings", token_data: dict[str, Any]) -> dict[str, Any]:
        """Refresh an expired token. Raises ``ReauthRequired`` if the platform
        has revoked it and the user must reconnect."""
        ...

    def publish(self, token_data: dict[str, Any], req: PublishRequest, *,
               on_progress: ProgressFn | None = None, cancel_check: CancelFn | None = None) -> PublishResult:
        """Upload ``req.file_path`` and report back what actually happened.
        Raises ``PlatformNotSupported`` for something this adapter genuinely
        can't do yet (e.g. TikTok Direct Post without an audited app) —
        never silently degrades the result's ``status`` instead."""
        ...
