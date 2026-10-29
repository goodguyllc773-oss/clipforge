# Handoff (ClipForge)

Read this first, every time you work on ClipForge, before touching anything else.
Update it before you stop, and immediately before any context compaction (see
CLAUDE.md, "ClipForge: HANDOFF.md", for the exact rule).

_Last updated 2026-10-29 (local session, Windows PC, RTX 2060 Super). **Where we
left off:** the full pipeline (import → transcribe → analyze → review → render →
export) is verified working end-to-end on real hardware against a real
43-minute YouTube video. Import-by-URL (YouTube/TikTok/direct links) and a new
Publishing feature (YouTube + TikTok, OAuth, auto-publish) were both built this
session. **⚠ A large amount of work is uncommitted** — see "Uncommitted work"
below. Don't assume `git log` on `main` reflects everything described here;
check `git status` first._

## Step zero, every session: fetch before trusting this file

**First: is this session on the user's computer?** If the "[ClipForge location
check]" line says CLOUD, stop. Tell the user, and send them to a local session
on `C:\Users\xalex\clipforge` (see CLAUDE.md, "Step 0").

**Before reading anything else, run `git status` and `git fetch origin`.** The
SessionStart hook (`scripts/repo-sync.mjs`) does this automatically and reports
"[ClipForge repo sync …]" — but it only acts when the tree is clean, and right
now it probably isn't (see below). Check `git status` yourself too.

## Repo state (check this before believing any of the rest of this file)

- **Repo:** `goodguyllc773-oss/clipforge` (public, on GitHub for real now — the
  "create the repo" job from earlier is done), branch `main`.
- **Pushed to `main`:** the import-by-URL feature (`bdc4a6a`, merged from
  `clipforge/url-import`, PR #1).
- **Uncommitted in this checkout** (`git status` to confirm — this list will go
  stale the moment someone commits):
  - Delete-project feature (backend `DELETE /api/projects/{id}` + cascade
    cleanup, frontend `ConfirmButton` component — native `confirm()` is
    suppressed in the Claude desktop app's browser pane, so a custom two-click
    confirm is used everywhere instead of `window.confirm`).
  - Accent color changed from orange to green (`frontend/src/styles.css`).
  - Two real bugs found and fixed in import-by-URL after merging it:
    1. `yt-dlp` pinned `<2026` went stale and YouTube started 403ing mid-download
       — unpinned the upper bound (`backend/pyproject.toml`), documented why.
    2. The download progress bar froze at 90% on large/fragmented downloads:
       yt-dlp's `total_bytes_estimate` is recomputed from the *current*
       fragment's size each call, so while one large fragment is mid-download
       the estimate (and the % derived from it) sits still even though bytes
       keep flowing. Fixed in `media/download.py` to only trust a genuine
       `total_bytes`, falling back to a byte-count "creep" otherwise
       (`jobs/handlers.py::handle_import_url`).
  - `beam_size` default dropped from 5 to 1 in `transcription/whisper.py`
    (greedy decoding) — roughly halves transcription wall time with word
    timestamps on, little accuracy loss on clear speech. User-requested after
    watching a real transcription run far slower than expected.
  - **Publishing** (new this session, D14 below) — the biggest uncommitted
    piece: `clipforge/publishing/` (adapter interface + real YouTube + TikTok
    adapters), migration `0003_publishing.py`, the `publish` job lane/handler,
    `/api/publish/*` routes, Settings → Integrations UI, Review page "Publish"
    box, a Publishing Queue page, `tests/test_publishing.py` (20 tests, all
    mocked), and new dependencies (`google-api-python-client`, `google-auth`,
    `google-auth-oauthlib`, `keyring`).
- **Only commit when the user asks** (CLAUDE.md rule) — none of the above has
  been committed. When they do ask: review `git status`/`git diff` first (a lot
  of unrelated-looking changes landed in one session), probably as more than
  one commit (delete-project+accent / bugfixes / publishing are logically
  separate), then push.

## Current state

- **Where it runs:** this Windows PC only, verified. Dev servers:
  `explorer.exe start-dev.cmd` → API+worker on :8765, hot-reload UI on :5173.
  **The backend does not hot-reload** — `clipforge serve` runs without
  `--reload` (`scripts/dev.mjs`), so any backend `.py` change needs the dev
  servers restarted (frontend changes hot-reload fine via Vite). Restarting:
  find the PIDs on :8765/:5173 (`Get-NetTCPConnection` in PowerShell),
  `Stop-Process -Force`, then relaunch via `explorer.exe start-dev.cmd` again —
  never as a plain background Bash/PowerShell process (gotcha #1 below).
- **Database:** migrations `0001_initial_schema`, `0002_source_url`,
  `0003_publishing` (uncommitted). Applied automatically on start.
  `alembic check` → no drift, verified.
- **Tests:** `147 passed, 1 skipped` (full suite, this PC, with ffmpeg on PATH
  — the winget install isn't on this shell's PATH by default, prepend
  `AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin`).
  Frontend `npm run typecheck` / `npm run build` clean.
- **Real hardware confirmed, not just detected:**
  - CUDA: faster-whisper transcription actually ran on `cuda` (not just
    `nvidia-smi` / diagnostics saying so).
  - NVENC: a real render's `encoder` field read `h264_nvenc`.
  - A real ~43-minute YouTube video (1080p, 1.5 GB) imported via paste-a-link,
    transcribed for real (5 chunks, `faster-whisper`/`small`/`cuda`), analyzed
    into 20 scored candidates with real transcript-grounded reasons, one
    approved and rendered.
- **Publishing: code-complete, mocked-tested only.** No real YouTube or TikTok
  account has been connected yet — that needs a registered developer app on
  each platform (README "Connecting YouTube/TikTok"), which the user hasn't
  set up. Don't claim it works against a real account until that's done.

## Gotchas (keep reading before you hit these)

- **#1 (Windows): AppData isn't shared with Claude's sandbox.** ClipForge
  keeps its DB/renders/models in `%LOCALAPPDATA%\ClipForge`. Always start
  servers/setup via `explorer.exe <script>.cmd`, never a plain tool-call
  process, or you get a second, empty data dir.
- **#2: the backend does not hot-reload.** See "Current state" above — this
  bit twice this session (a new API route returned 405 until the servers were
  restarted; a code fix for the progress-bar bug didn't take effect until
  restarted either). Always restart after a backend change, before claiming a
  fix is "live".
- **#3:** `CLIPFORGE_TRANSCRIBER=sidecar` reads `.srt/.vtt/.json` next to the
  video instead of running Whisper — how tests/cloud sessions work without a
  model. Default is `faster-whisper`.
- **#4:** in a Bash tool call, `pkill -f` / `pgrep -f` match the tool's own
  shell command line and kill it. Use PowerShell `Get-CimInstance Win32_Process`
  + `Stop-Process -Id` by PID instead, or a `[b]racket` pattern.
- **#5:** this Windows shell's PATH doesn't include ffmpeg by default even
  after `winget install Gyan.FFmpeg` — prepend the WinGet package bin dir (see
  "Tests" above) for any `pytest` run from a tool call.
- **#6:** MCC's setup recognises its own hook by `sync-check.mjs` in the
  command; ClipForge's script is `repo-sync.mjs` on purpose — don't rename it.
- **#7:** schema changes: `alembic -c alembic.ini upgrade head` on a throwaway
  `dev-clipforge.db`, then `revision --autogenerate`, then review and rename
  the generated file to the next `000N_name.py` (autogenerate names it by hash).

## Last verified working (2026-10-29, this session, Windows PC)

- **Full pipeline, real video, real hardware** — see "Current state" above for
  the summary; this is the first time any of milestone 1 ran outside a cloud
  container.
- **Import-by-URL, real YouTube:** paste-a-link → `yt-dlp` → real download →
  import, after fixing the two bugs listed under "Uncommitted work" above.
- **Delete-project:** backend cascade-deletes media/transcripts/candidates/
  renders and the project's render-output folder; refuses while a job is
  active; frontend two-click confirm verified live in the browser pane (native
  `confirm()` silently no-ops there — don't use `window.confirm` anywhere in
  this app, use `components/ConfirmButton.tsx`).
- **Publishing — mocked only:** `test_publishing.py`, 20/20 passing — account
  connect/reconnect/disconnect, request/cancel/retry, dedup of an active
  publish for the same render+account, `ReauthRequired` flips the account to
  `needs_reauth`, `PlatformNotSupported` (TikTok non-private) reports
  `needs_action` without being retried like a transient failure, a `draft`
  result is never reported as `published`, and restart recovery (crash
  mid-upload → requeued → finishes on the next worker start) using the same
  simulated-crash technique as `test_jobs.py`. UI verified live in the browser:
  Settings → Integrations empty/configured states, Publishing Queue empty
  state, the Review page's Publish box showing "no connected account" correctly
  for the one real render that exists.
- **Found and fixed while building publishing:** `ExportRecord` had no `render`
  relationship despite `RenderedClip.exports` declaring `back_populates="render"`
  — a latent bug that happened to never trigger SQLAlchemy's mapper validation
  until new relationships were added elsewhere in the same module and forced
  full registry configuration. Fixed (`db/models.py`); covered by the existing
  test suite now actually exercising it.

## Decisions (made by Claude, NOT asked or confirmed by the user unless noted)

All recorded with reasons in `docs/decisions.md` (D1–D14):

- The UI is a localhost web page for now; Tauri was deferred.
- A SQLite job queue (no Redis/Celery).
- Default Whisper model `small`; `beam_size=1` (user asked for this one).
- Approving a clip auto-renders it; export needs approval.
- Silent videos get scene-based "weak" candidates and a silent audio track.
- D13: import-by-URL is a scoped exception to "no outbound calls".
- D14: publishing is a scoped exception to "no automatic publishing" — real
  OAuth only, tokens in the OS keychain, `auto_publish` off by default per
  account, TikTok limited to private drafts until the app is audited.

## Not verified yet / known limits

1. **Publishing against real accounts.** Everything is mocked-tested; no real
   YouTube or TikTok OAuth client has been registered yet. Next session with
   real credentials should do README's "Connecting YouTube/TikTok" then the
   manual tests in `docs/implementation-plan.md` ("Publishing, real accounts").
2. **Scheduled publishing isn't honoured.** `PublishJob.scheduled_for` exists
   on the model/API; neither adapter reads it — both publish immediately.
3. Real Whisper / NVENC are now verified (see above) — the old "never run on
   Windows" caveats from the pre-repo-move era no longer apply.
4. Face framing untested on real faces beyond the one MrBeast video (worked
   there). Still a fixed per-clip crop, no tracking during the clip.
5. **Clip ranking is a heuristic baseline (`heuristic-v1`).** No semantic
   understanding — the UI and this file both say so on purpose.
6. No speaker detection; facecam/split-screen regions are set by hand.
7. SRT/VTT imports have estimated word timings (drift within a line).
8. **No committed UI test** beyond today's manual browser-pane verification.
9. TikTok adapter code is written against the documented API shape but has
   never hit TikTok's real servers.

## What's next

1. **Decide what to commit and push.** See "Uncommitted work" above — probably
   several logically separate commits, not one.
2. **Publishing with real credentials** — see "Not verified yet" #1.
3. **Milestone 2** (`docs/implementation-plan.md`): local semantic re-ranker
   via Ollama, a hand-labelled real-video eval set, speaker turns, moving face
   tracking, a timeline editor, the Tauri shell, a committed Playwright test.
4. **Milestone 3 — Original Content Studio** (not started): script generation/
   editing/series-continuity, TTS narration adapters, a background-visual
   track system, reusing today's render/caption engine for original
   (non-clipped) short-form video. A separate, large piece of work — came up
   in the same conversation that asked for publishing, deliberately deferred.
5. **Milestone 4 — Campaign Center** (not started): paid-clipping campaign
   records, campaign-aware clip requirements, submission tracking, estimated-
   vs-confirmed earnings analytics. Also deferred.
