# ClipForge

Local-first clipping: import a long video (podcast, stream VOD, reaction, commentary,
gaming, vlog), transcribe it **on your machine**, get a ranked review queue of
30–60 s moments with the reasons they were picked, then render vertical 1080×1920
MP4s with animated word-by-word captions. Nothing is uploaded and nothing is
published automatically.

**Working on ClipForge? Read `HANDOFF.md` first** (current state, what's verified,
what's next).

Status: milestone 1 (working vertical slice). See `docs/implementation-plan.md` for
exactly what is done, what's only partly verified, and what's next.

## Install (Windows 10/11)

Prerequisites: Python 3.10+ (3.12 recommended), FFmpeg, and Node.js LTS for the UI
build. The setup script checks each one and tells you the `winget` command if it's
missing.

```bat
git clone https://github.com/goodguyllc773-oss/clipforge.git
cd clipforge
setup.cmd          :: add -Gpu to install CUDA libs for GPU transcription without asking
start.cmd          :: opens http://127.0.0.1:8765/
start-dev.cmd      :: dev servers instead: API :8765 + hot-reload UI :5173
```

`setup.cmd` creates `backend\.venv`, installs the Python packages there, offers the
NVIDIA cuBLAS/cuDNN wheels if it sees an NVIDIA GPU, builds the UI and runs
`clipforge doctor`.

**First transcription downloads the Whisper model** (`small` ≈ 460 MB) from
huggingface.co into `%LOCALAPPDATA%\ClipForge\models`. After that it works offline.

### GPU notes (RTX 2060 Super, 8 GB)
* Transcription: `small` (default) uses ~1 GB VRAM; `medium` or `distil-large-v3`
  (English) are more accurate and still fit. Change in Settings.
* If CUDA libraries are missing, transcription falls back to CPU and the job log
  says so. Fix: `backend\.venv\Scripts\python -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"`.
* Rendering uses NVENC only if a real test encode succeeds (Settings → Diagnostics),
  otherwise libx264.

## Install (macOS / Linux, or manual)

```bash
git clone https://github.com/goodguyllc773-oss/clipforge.git && cd clipforge
scripts/setup-mac.sh                # venv, packages, UI build, Claude sync hook, doctor
open start-dev.command              # dev servers (or: npm run dev)
```

## Use

1. **Library** → create a project (pick a default editing profile).
2. **Import video** → browse to the file (or paste its full path), or switch to
   **Paste a link** to import from a direct video URL or a YouTube/TikTok/etc. link
   (fetched with `yt-dlp`; see "Import by URL" below). Optionally attach an
   existing `.srt` / `.vtt` to skip Whisper. ClipForge never copies or modifies a
   local source; a pasted link is downloaded once and then treated the same way.
3. Wait for *Transcribing* → *Finding clips* (progress bars; cancellable).
4. **Review clips**: watch the source range or the render, read *Why this clip*,
   nudge boundaries, change profile / layout / caption style, **Approve** (renders
   automatically) or **Reject**. Keys: `J`/`K` next/prev, `A` approve, `X` reject.
5. **Export** an approved render to a folder: you get the MP4 plus `.json` (source
   timestamps, transcript, reasons, hashtags) and `.txt` (ready-to-paste caption).

Only clip footage you have the rights to edit and post.

## Develop

```bash
# API + worker on :8765 and hot-reload UI on :5173, in one window (Ctrl+C stops both)
npm run dev
# keep GitHub and this machine in step (also runs at every Claude session start)
npm run sync
# tests (needs ffmpeg/ffprobe on PATH; ~2 min)
cd backend && .venv/bin/python -m pytest      # Windows: .venv\Scripts\python -m pytest
# real-Whisper test (needs network the first time):
CLIPFORGE_TEST_WHISPER_MODEL=tiny.en python -m pytest -m whisper
```

Environment variables: `CLIPFORGE_DATA_DIR`, `CLIPFORGE_FFMPEG`, `CLIPFORGE_FFPROBE`,
`CLIPFORGE_TRANSCRIBER` (`faster-whisper` | `sidecar`), `CLIPFORGE_PORT`,
`CLIPFORGE_EMBEDDED_WORKER` (`0` → run `python -m clipforge worker` separately).

Data lives in `%LOCALAPPDATA%\ClipForge` (Windows) / `~/.local/share/clipforge`
(Linux) / `~/Library/Application Support/ClipForge` (macOS): `clipforge.db`, `logs/`,
`models/`, `cache/`, `renders/`.

Docs: `docs/architecture.md` (modules, data model, job state machine),
`docs/decisions.md` (why things are the way they are),
`docs/implementation-plan.md` (status checklist, tests, next milestone).

## Import by URL

Paste a link instead of browsing to a file. A direct link to a video file (ending
`.mp4`, `.mov`, etc.) is downloaded straight to disk; anything else (a YouTube,
TikTok, or other page URL) is resolved and fetched with `yt-dlp`, restricted to
the single link pasted — never a whole channel or playlist. The download runs as
a background job (Jobs page shows its progress) and, once it completes, feeds
into the exact same pipeline as a locally-browsed file. This is the one place
ClipForge makes an outbound network call other than the one-time Whisper model
download — see `docs/decisions.md` D13 for the reasoning and what's still true
(no scraping, no scheduled or automatic fetching).

`yt-dlp` is installed automatically by `setup.cmd` / `scripts/setup-mac.sh` (it's
a regular backend dependency, in `backend/pyproject.toml`); no extra prerequisite
to install by hand.

## Publishing (YouTube / TikTok)

Approve a clip, it renders, and — once you've connected an account and (optionally)
turned on auto-publish for it — it can go straight to YouTube or TikTok with no
further clicks. See `docs/decisions.md` D14 for the full design. Nothing here
works without you first registering your own app with each platform; ClipForge
never ships or embeds platform credentials.

### Connecting YouTube

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project
   (or use an existing one) and enable the **YouTube Data API v3**
   (APIs & Services → Library).
2. APIs & Services → Credentials → **Create OAuth client ID**, type **Web
   application**. Add an authorized redirect URI:
   `http://127.0.0.1:8765/api/publish/youtube/callback` (match
   `CLIPFORGE_OAUTH_REDIRECT_BASE` below if you change it).
3. Copy the Client ID and Client Secret and set them before starting the server:
   `CLIPFORGE_YOUTUBE_CLIENT_ID=...` and `CLIPFORGE_YOUTUBE_CLIENT_SECRET=...`.
4. Restart ClipForge, then Settings → Integrations → YouTube → Connect account.
   Your Google account's OAuth consent screen opens in your own browser — sign in
   there, not inside ClipForge.
5. While your OAuth client is in Google's "Testing" publishing status, only
   Google accounts you've added as test users can complete this — add yourself
   under OAuth consent screen → Test users.

### Connecting TikTok

1. Register an app at [TikTok for Developers](https://developers.tiktok.com/)
   and add the **Content Posting API** product.
2. Add a redirect URI: `http://127.0.0.1:8765/api/publish/tiktok/callback`.
3. Set `CLIPFORGE_TIKTOK_CLIENT_KEY=...` and `CLIPFORGE_TIKTOK_CLIENT_SECRET=...`,
   restart, then Settings → Integrations → TikTok → Connect account.
4. **Until TikTok approves your app's Content Posting API access**, published
   clips land as a **private draft** in your TikTok inbox — open the TikTok app
   yourself to add a caption and post it. This is TikTok's own platform policy
   for unaudited apps, not a ClipForge limitation, and ClipForge never reports a
   draft as "published".

## Known limitations

* Clip ranking is a transparent **heuristic baseline** (hook / finished thought /
  self-contained start / delivery energy …). It does not understand humour or story
  payoff. A local-LLM re-ranker is the next milestone.
* No speaker diarization; split-screen regions and facecam rectangles are set by hand.
* Face framing is a fixed per-clip crop from OpenCV's frontal-face detector — no
  tracking during the clip, misses profile views.
* SRT/VTT imports have estimated (interpolated) word timings, so the highlight can
  drift slightly within a line.
* Whisper and NVENC paths were not exercised on real hardware in the build
  environment; see "Manual tests" in the plan.
