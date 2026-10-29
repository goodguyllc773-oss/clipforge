"""TikTok Content Posting API adapter: OAuth 2.0 and the upload-to-inbox
(draft) workflow, which — unlike Direct Post — doesn't require TikTok's app
audit. A video uploaded this way lands as a **private draft** in the
creator's TikTok inbox; they open the TikTok app themselves to finish posting
it. That's TikTok's own platform policy for unaudited apps, not a ClipForge
limitation, and this adapter never reports a draft as "published".

Real documented API calls (stdlib ``urllib``, no extra HTTP dependency, same
style as ``media/download.py``) — **not yet exercised against a real TikTok
app**, since that needs the developer's own registered app (see README
"Connecting TikTok"). Direct Post (publishing straight to a creator's
feed) needs TikTok's Content Posting API audit; until an app has that
approval, :meth:`publish` only offers the inbox/draft path and raises
``PlatformNotSupported`` for anything that would imply more.

Docs: https://developers.tiktok.com/docs/en/content-posting-api-get-started
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..errors import PlatformNotSupported, PublishFailed, ReauthRequired, ValidationFailed
from .base import AccountInfo, CancelFn, ProgressFn, PublishRequest, PublishResult

if TYPE_CHECKING:
    from ..config import Settings

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = ["user.info.basic", "video.upload", "video.publish"]
CHUNK = 10 * 1024 * 1024  # TikTok's documented minimum chunk size is 5 MiB


def _creds(s: "Settings"):
    if not s.tiktok_client_key or not s.tiktok_client_secret:
        raise ValidationFailed(
            "TikTok isn't configured on this machine.",
            hint="Set CLIPFORGE_TIKTOK_CLIENT_KEY / CLIPFORGE_TIKTOK_CLIENT_SECRET "
                 "(see README \"Connecting TikTok\") and restart the server.")
    return s.tiktok_client_key, s.tiktok_client_secret


def _post_json(url: str, body: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise PublishFailed(f"TikTok API error {e.code}: {detail[:300]}", retryable=e.code >= 500) from e
    except URLError as e:
        raise PublishFailed(f"Couldn't reach TikTok: {e.reason}.", retryable=True) from e


class TikTokAdapter:
    name = "tiktok"

    def authorize_url(self, settings: "Settings", redirect_uri: str, state: str) -> str:
        key, _ = _creds(settings)
        q = urlencode({"client_key": key, "scope": ",".join(SCOPES), "response_type": "code",
                      "redirect_uri": redirect_uri, "state": state})
        return f"{AUTH_URL}?{q}"

    def exchange_code(self, settings: "Settings", redirect_uri: str, code: str) -> tuple[dict[str, Any], AccountInfo]:
        key, secret = _creds(settings)
        body = urlencode({"client_key": key, "client_secret": secret, "code": code,
                          "grant_type": "authorization_code", "redirect_uri": redirect_uri}).encode()
        req = Request(TOKEN_URL, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                token_data = json.loads(resp.read())
        except HTTPError as e:
            raise PublishFailed(f"TikTok sign-in failed: {e.read().decode(errors='replace')[:300]}",
                                retryable=False) from e
        if "access_token" not in token_data:
            raise PublishFailed(f"TikTok sign-in failed: {token_data}", retryable=False)
        info = AccountInfo(external_account_id=token_data.get("open_id", ""),
                           display_name=token_data.get("open_id", "TikTok account"), scopes=SCOPES)
        return token_data, info

    def refresh(self, settings: "Settings", token_data: dict[str, Any]) -> dict[str, Any]:
        key, secret = _creds(settings)
        body = urlencode({"client_key": key, "client_secret": secret,
                          "grant_type": "refresh_token", "refresh_token": token_data.get("refresh_token", "")}).encode()
        req = Request(TOKEN_URL, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                refreshed = json.loads(resp.read())
        except HTTPError as e:
            raise ReauthRequired("TikTok's token could not be refreshed.",
                                 hint="Reconnect the account in Settings → Integrations.") from e
        if "access_token" not in refreshed:
            raise ReauthRequired("TikTok's token could not be refreshed.",
                                 hint="Reconnect the account in Settings → Integrations.")
        return {**token_data, **refreshed}

    def publish(self, token_data: dict[str, Any], req: PublishRequest, *,
               on_progress: ProgressFn | None = None, cancel_check: CancelFn | None = None) -> PublishResult:
        if req.privacy != "private":
            raise PlatformNotSupported(
                "This app isn't audited for TikTok Direct Post, so it can only send a clip to your "
                "TikTok inbox as a private draft — not post it publicly.",
                hint="Finish it in the TikTok app: Inbox → the uploaded draft → fill in the caption → Post. "
                     "Or request Direct Post access from TikTok and reconnect once it's approved.")
        token = token_data.get("access_token")
        size = Path(req.file_path).stat().st_size
        chunk_count = max(1, (size + CHUNK - 1) // CHUNK)
        init = _post_json(INBOX_INIT_URL, {
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                           "chunk_size": min(CHUNK, size), "total_chunk_count": chunk_count},
        }, token=token)
        data = init.get("data") or {}
        upload_url, publish_id = data.get("upload_url"), data.get("publish_id")
        if not upload_url or not publish_id:
            raise PublishFailed(f"TikTok didn't return an upload target: {init}", retryable=True)

        with open(req.file_path, "rb") as f:
            sent = 0
            for i in range(chunk_count):
                if cancel_check:
                    cancel_check()
                piece = f.read(CHUNK)
                start, end = sent, sent + len(piece) - 1
                r = Request(upload_url, data=piece, method="PUT", headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"})
                try:
                    with urlopen(r, timeout=120):
                        pass
                except HTTPError as e:
                    raise PublishFailed(f"TikTok upload chunk failed: {e.code}", retryable=True) from e
                sent += len(piece)
                if on_progress:
                    on_progress(min(1.0, sent / size))

        return PublishResult(status="draft", platform_upload_id=publish_id,
                             note="Sent to your TikTok inbox as a private draft — open the TikTok app "
                                  "to add a caption and post it (unaudited apps can't post directly).")

    def check_status(self, token_data: dict[str, Any], publish_id: str) -> dict[str, Any]:
        """Not called by the generic pipeline yet (draft status doesn't change
        server-side after inbox delivery) — here for the future poller the
        publishing-UI phase wires up."""
        return _post_json(STATUS_URL, {"publish_id": publish_id}, token=token_data.get("access_token"))
