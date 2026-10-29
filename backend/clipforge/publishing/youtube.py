"""YouTube Data API v3 adapter: OAuth 2.0 (server-side auth-code flow) and a
resumable upload via ``videos.insert``. Real network calls, no browser
automation, no scraping — the official API only.

Setup (one-time, per machine — see README "Connecting YouTube"):
1. Create a Google Cloud project, enable the YouTube Data API v3.
2. Create an OAuth 2.0 client (type "Web application"), with authorized
   redirect URI ``<CLIPFORGE_OAUTH_REDIRECT_BASE>/api/publish/youtube/callback``.
3. Set ``CLIPFORGE_YOUTUBE_CLIENT_ID`` / ``CLIPFORGE_YOUTUBE_CLIENT_SECRET``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import DependencyMissing, PublishFailed, ReauthRequired, ValidationFailed
from .base import AccountInfo, CancelFn, ProgressFn, PublishRequest, PublishResult

if TYPE_CHECKING:
    from ..config import Settings

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
_PRIVACY = {"private", "unlisted", "public"}


def _flow(s: "Settings", redirect_uri: str):
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as e:
        raise DependencyMissing("google-auth-oauthlib isn't installed.",
                                hint="Re-run setup — it's part of the backend package.") from e
    if not s.youtube_client_id or not s.youtube_client_secret:
        raise ValidationFailed(
            "YouTube isn't configured on this machine.",
            hint="Set CLIPFORGE_YOUTUBE_CLIENT_ID / CLIPFORGE_YOUTUBE_CLIENT_SECRET "
                 "(see README \"Connecting YouTube\") and restart the server.")
    client_config = {
        "web": {
            "client_id": s.youtube_client_id,
            "client_secret": s.youtube_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)


class YouTubeAdapter:
    name = "youtube"

    def authorize_url(self, settings: "Settings", redirect_uri: str, state: str) -> str:
        flow = _flow(settings, redirect_uri)
        url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true",
                                        prompt="consent", state=state)
        return url

    def exchange_code(self, settings: "Settings", redirect_uri: str, code: str) -> tuple[dict[str, Any], AccountInfo]:
        flow = _flow(settings, redirect_uri)
        flow.fetch_token(code=code)
        creds = flow.credentials
        token_data = {
            "token": creds.token, "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri, "client_id": creds.client_id,
            "client_secret": creds.client_secret, "scopes": creds.scopes,
        }
        info = self._fetch_channel(creds)
        return token_data, info

    def refresh(self, settings: "Settings", token_data: dict[str, Any]) -> dict[str, Any]:
        creds = self._credentials_from(token_data)
        from google.auth.transport.requests import Request as GoogleRequest
        try:
            creds.refresh(GoogleRequest())
        except Exception as e:
            raise ReauthRequired("YouTube's token could not be refreshed.",
                                 hint="Reconnect the account in Settings → Integrations.") from e
        token_data = dict(token_data)
        token_data["token"] = creds.token
        return token_data

    def publish(self, token_data: dict[str, Any], req: PublishRequest, *,
               on_progress: ProgressFn | None = None, cancel_check: CancelFn | None = None) -> PublishResult:
        if req.privacy not in _PRIVACY:
            raise ValidationFailed(f"Invalid privacy '{req.privacy}'.", hint=f"Use one of {sorted(_PRIVACY)}.")
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        creds = self._credentials_from(token_data)
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {"title": req.title[:100] or "Untitled clip", "description": req.description[:5000],
                       "tags": req.tags[:500], "categoryId": "22"},
            "status": {"privacyStatus": req.privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(req.file_path), chunksize=8 * 1024 * 1024, resumable=True,
                                mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            if cancel_check:
                cancel_check()
            try:
                status, response = request.next_chunk()
            except HttpError as e:
                raise PublishFailed(f"YouTube upload failed: {e.reason if hasattr(e, 'reason') else e}",
                                    retryable=e.resp.status >= 500 if hasattr(e, "resp") else True) from e
            if status and on_progress:
                on_progress(min(1.0, status.progress()))
        video_id = response["id"]
        return PublishResult(status="uploaded", platform_upload_id=video_id, platform_post_id=video_id,
                             platform_post_url=f"https://youtu.be/{video_id}",
                             note="Uploaded. YouTube may take a few minutes to finish processing it.")

    # ------------------------------------------------------------------
    def _credentials_from(self, token_data: dict[str, Any]):
        from google.oauth2.credentials import Credentials
        return Credentials(token_data.get("token"), refresh_token=token_data.get("refresh_token"),
                          token_uri=token_data.get("token_uri"), client_id=token_data.get("client_id"),
                          client_secret=token_data.get("client_secret"), scopes=token_data.get("scopes"))

    def _fetch_channel(self, creds) -> AccountInfo:
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items") or []
        if not items:
            raise PublishFailed("This Google account has no YouTube channel.",
                                hint="Create a channel on this account, then reconnect.", retryable=False)
        ch = items[0]
        thumb = (ch["snippet"].get("thumbnails") or {}).get("default", {}).get("url")
        return AccountInfo(external_account_id=ch["id"], display_name=ch["snippet"]["title"],
                          thumbnail_url=thumb, scopes=list(creds.scopes or []))
