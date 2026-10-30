# Handoff (ClipForge)

Read this first, every time you work on ClipForge, before touching anything else.
Update it before you stop, and immediately before any context compaction (see
CLAUDE.md, "ClipForge: HANDOFF.md", for the exact rule).

_Last updated 2026-10-30 (local session, Windows PC, RTX 2060 Super), end of
session / closeout. **TikTok publishing is now verified working end to end**,
including a real upload: OAuth connect → `PublishAccount` row created → a real
rendered clip uploaded via the Content Posting API → landed as a private draft
in the TikTok inbox (`platform_upload_id: v_inbox_file~v2.7702405277987014669`,
status `draft`, confirmed both via `GET /api/publish/jobs` and the Publishing
Queue page in the UI). Along the way, found and fixed a real chunking bug (see
"TikTok: verified working end to end" below). **Everything from this session
is committed and pushed** — `89851e0` on `main`, working tree clean, nothing
pending._

## Step zero, every session: fetch before trusting this file

**First: is this session on the user's computer?** If the "[ClipForge location
check]" line says CLOUD, stop. Tell the user, and send them to a local session
on `C:\Users\xalex\clipforge` (see CLAUDE.md, "Step 0").

**Before reading anything else, run `git status` and `git fetch origin`.** The
SessionStart hook (`scripts/repo-sync.mjs`) only acts when the tree is clean,
and right now it isn't — check `git status` yourself.

## Repo state (check this before believing any of the rest of this file)

- **Repo:** `goodguyllc773-oss/clipforge` (public), branch `main`.
- **Pushed to `main`, `git status` clean:** `5f58ce9` (delete-project/accent/
  bugfixes), `7ce692b` (publishing: YouTube+TikTok adapters, queue, UI,
  delete-project backend), and `89851e0` (this session — `.env` wiring, the
  TikTok chunking fix, display-name fetch, test-isolation fix; see commit
  message for the full breakdown). Nothing uncommitted right now.
- **What `89851e0` actually changed**, for context on why things look the way
  they do:
  - `.gitignore`: `.env.*` was also swallowing `.env.example` (the committed,
    secret-free template) — fixed with a `!.env.example` exception.
  - `backend/clipforge/config.py`: loads a gitignored `.env` from the repo root
    automatically (`python-dotenv`, new direct dependency — was only ever a
    transitive one before); added `tiktok_redirect_base` +
    `Settings.redirect_base(platform)` (TikTok needs a different OAuth redirect
    base than YouTube — see below); wired the previously-dead
    `extra_allowed_origins` setting into the actual `TrustedHostMiddleware`.
  - `backend/clipforge/api/app.py`: both OAuth routes now call
    `settings.redirect_base(platform)` instead of the single global
    `oauth_redirect_base`; `TrustedHostMiddleware`'s `allowed_hosts` now
    includes `effective_settings.extra_allowed_origins`.
  - `backend/clipforge/publishing/tiktok.py`: dropped `video.publish` from
    `SCOPES` — that scope is Direct-Post-only and gets rejected for an
    unaudited app; this adapter only ever needs `user.info.basic` +
    `video.upload`. Found by actually registering the app and reading what
    TikTok itself granted.
  - `backend/clipforge/publishing/tiktok.py` (second fix, found via a real
    upload attempt): chunk sizing was a fixed 10 MiB, which left a tiny
    leftover final chunk for a file that doesn't divide evenly (a 50.2 MB
    render → 5×10 MiB + one ~256 KB chunk) — TikTok's real API rejected that
    with `"The total chunk count is invalid"`. Replaced with `_chunk_plan()`:
    single chunk (whole file) when size ≤ 64 MiB, else evenly-split chunks in
    TikTok's documented 5–64 MiB bounds. Covered by a new test
    (`test_tiktok_chunk_plan_never_leaves_an_undersized_trailing_chunk` in
    `test_publishing.py`).
  - `backend/clipforge/publishing/tiktok.py` (third change, cosmetic): after
    token exchange, now calls TikTok's `/v2/user/info/` to fetch a real
    `display_name`/`avatar_url` instead of showing the raw `open_id`. Best
    effort — falls back to `open_id` silently if that call fails. **Not yet
    exercised**: the currently-connected test account was connected before
    this landed, so it still shows as the raw ID
    (`-000rKcUIAXAj0jWlHBiK65hEIspolLGEUnC`) in Settings; only takes effect on
    the next connect/reconnect, which hasn't happened since (the account
    already proved working, so reconnecting wasn't forced just for this).
  - `backend/tests/conftest.py` + `test_publishing.py`: the `ctx` fixture now
    explicitly clears all publishing credential fields — a real `.env` was
    leaking into "is this unconfigured" test assertions once `.env` support
    landed. One test simplified now that the fixture guarantees this itself.
  - `README.md`: "Connecting YouTube/TikTok" now says to use `.env` (copied
    from `.env.example`) instead of vague "set it before starting the server".
  - `.env.example` (new, committed): template for the OAuth env vars, no real
    values.
- **`.env` exists at the repo root, is gitignored, and has real values in it
  right now** — TikTok Sandbox credentials, an ngrok redirect base, and the
  extra-allowed-origins entry (see "Publishing: live TikTok sandbox setup"
  below for what's actually in it; don't print its contents into chat or a
  commit).

## Current state

- **Where it runs:** this Windows PC only, verified. Dev servers:
  `explorer.exe start-dev.cmd` → API+worker on :8765, hot-reload UI on :5173.
  **The backend does not hot-reload** — `clipforge serve` runs without
  `--reload`, so any backend `.py` change (or `.env` change — it's only read
  once, at startup) needs the dev servers restarted. Restarting: find the PIDs
  on :8765/:5173 (`Get-NetTCPConnection` in PowerShell), `Stop-Process -Force`,
  then relaunch via `explorer.exe start-dev.cmd` — never as a plain background
  Bash/PowerShell process (gotcha #1 below). Currently running with the
  Sandbox `.env` values loaded.
- **Database:** migrations `0001_initial_schema`, `0002_source_url`,
  `0003_publishing`. Applied automatically on start. `alembic check` → no drift.
- **Tests:** full suite re-run after the chunk-plan fix: `148 passed, 1
  skipped` (147 + the new chunk-plan test), this PC (ffmpeg needs prepending
  to PATH for a tool-call `pytest` run — gotcha #5). Frontend
  `npm run typecheck` clean (no frontend changes this session, `build` not
  separately re-run but typecheck covers the relevant risk).
- **Real hardware confirmed:** CUDA transcription, NVENC render, a real
  43-minute YouTube video imported/transcribed/analyzed/rendered end to end —
  see "Last verified working" in git history of this file if needed; not
  re-detailed here to keep this file current rather than exhaustive.
- **Publishing — YouTube: code-complete, never connected.** No Google Cloud
  OAuth client has been registered yet (`CLIPFORGE_YOUTUBE_CLIENT_ID/SECRET`
  are empty in `.env`). Google supports `http://127.0.0.1` redirects natively,
  so no tunnel will be needed for YouTube — just follow README "Connecting
  YouTube" when there's a credential to put in.
- **Publishing — TikTok: fully verified.** Live Sandbox app, account
  connected, one real clip uploaded and confirmed as a private draft. See
  next section.

## Publishing: live TikTok sandbox setup (do this before redoing any of it)

A real TikTok for Developers organization + app + sandbox now exist:

- **Organization:** "GoodGuyEx" (TikTok org ID `7702379815797720071`).
- **App:** "GoodGuyEx ClipForge" (app ID `7702367554412627975`), type "Other".
  Products: Login Kit + Content Posting API. The **Production** version of
  this app was started but never finished (missing ToS/Privacy URLs etc. —
  abandoned once TikTok's "localhost not supported" redirect-URI rejection
  showed up) — don't assume Production is usable; Sandbox is what's wired up.
- **Sandbox:** "local-dev" (sandbox ID `7702358365414017042`), cloned from the
  Production draft, then configured directly: Login Kit + Content Posting API
  added, scopes `user.info.basic` + `video.upload`, basic info filled in
  (icon, category "Entertainment", description, ToS/Privacy URLs — all
  pointing at the GitHub repo as a placeholder since this never gets
  submitted for TikTok's review), Web platform enabled, redirect URI set, one
  **Target User** authorized (`user386692113`, the developer's own TikTok
  account — required for Sandbox OAuth to work at all).
- **Its Client key/secret are in `.env`** as `CLIPFORGE_TIKTOK_CLIENT_KEY` /
  `_SECRET` (the `sb...` prefixed Sandbox ones — **not** the Production app's
  credentials, which were tried first and fail with
  `error_type=client_key`/`unauthorized_client` because Production's redirect
  URI/products were never actually saved).

### The hard constraint that drove all of this: TikTok rejects localhost

Unlike Google, **TikTok's redirect-URI validation flat-out rejects
`localhost`/`127.0.0.1`** — confirmed server-side on "Apply changes" with the
literal error `Redirect Uri: Enter a valid redirect uri (localhost is not
supported)`. No sandbox exception, no dev-mode flag. So testing the OAuth
round-trip against a real local ClipForge server needs the server reachable at
a real HTTPS hostname. Solution: an ngrok tunnel.

### ngrok tunnel (must be running for the TikTok flow to work at all)

- Installed via the **Microsoft Store** (`ngrok version` → `3.39.11-msix-stable`),
  **not** winget and **not** a directly-downloaded zip. Both of those got
  blocked by Windows Application Control ("Malicious binary reputation") —
  even running the user's own terminal, outside any Claude sandbox. The
  winget package's *original* install worked fine; it was `ngrok update`
  (which fetches a fresh binary directly) and a manual curl-download that both
  got blocked. The Store version carries Microsoft's own trust chain and
  isn't blocked. **If ngrok needs reinstalling, use the Store, not winget.**
  (Leftover winget package files/shims may still need cleanup — check
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_*` and
  `C:\Users\xalex\bin\ngrok.exe` if `ngrok` resolves to the wrong binary.)
- Free account signed in as `goodguyllc773@gmail.com`; authtoken configured
  (`ngrok config add-authtoken ...`, saved to
  `C:\Users\xalex\AppData\Local\ngrok\ngrok.yml`).
- Free static "dev domain": **`divorcee-cruncher-debit.ngrok-free.dev`**
  (ngrok dashboard → Domains). Using this instead of a random per-run
  subdomain means the TikTok redirect URI doesn't need re-registering every
  time the tunnel restarts.
- **Must be running** for TikTok OAuth to work: in a **user terminal** (not a
  Claude tool call — ngrok itself isn't blocked there, just launching it via
  the agent's own sandboxed Bash/PowerShell is), run:
  ```
  ngrok http --domain=divorcee-cruncher-debit.ngrok-free.dev 8765
  ```
  Verify with `curl https://divorcee-cruncher-debit.ngrok-free.dev/api/health`
  — if that fails with `Invalid host header`, the backend wasn't restarted
  after `.env`'s `CLIPFORGE_EXTRA_ALLOWED_ORIGINS` was added (see below).
- **ClipForge's own DNS-rebinding guard** (`TrustedHostMiddleware`) also had
  to be told to trust that hostname — that's what the new
  `extra_allowed_origins` wiring in `config.py`/`app.py` is for.
  `CLIPFORGE_EXTRA_ALLOWED_ORIGINS=divorcee-cruncher-debit.ngrok-free.dev` is
  set in `.env`.
- **`CLIPFORGE_TIKTOK_REDIRECT_BASE`** in `.env` points at the ngrok https URL;
  `CLIPFORGE_OAUTH_REDIRECT_BASE` (used by YouTube) stays
  `http://127.0.0.1:8765` — they're independent now
  (`Settings.redirect_base(platform)`), so fixing TikTok didn't touch YouTube.

### TikTok: verified working end to end

The full flow has now actually been clicked through and tested for real, not
just set up:

1. **OAuth connect completed.** User clicked Continue on TikTok's real consent
   screen. One snag: ngrok's free-tier interstitial ("You are about to visit
   divorcee-cruncher-debit.ngrok-free.dev... Visit Site") intercepted the
   callback redirect before it reached ClipForge — had to click "Visit Site"
   on that page too. (Sending an `ngrok-skip-browser-warning` header would
   avoid this for a non-browser client; irrelevant here since a real browser
   redirect is the whole point, but worth remembering if this trips someone
   up again — it only shows once per visitor per tunnel session.)
   Result: `GET /api/publish/accounts` → one row, `platform: "tiktok"`,
   `status: "connected"`, `external_account_id`/`display_name`:
   `-000rKcUIAXAj0jWlHBiK65hEIspolLGEUnC` (TikTok's `open_id` — see the
   display-name fix in "Uncommitted work", not yet re-tested), account id
   `fcf84ffd-457d-4f8a-bbaa-c12aa3d637a4`.
2. **A real publish was attempted** against the one existing rendered clip
   (MrBeast project, render `b3117f92-6f41-4afa-86bd-44940c1b1d79`, 47s,
   1080×1920, ~50.2 MB, h264_nvenc) via
   `POST /api/renders/{id}/publish` with `privacy: "private"`.
   - **First attempt failed**: TikTok's `inbox/video/init` returned
     `400 invalid_params: "The total chunk count is invalid"`. Root cause and
     fix: see the chunk-plan bug in "Uncommitted work" above.
   - **After the fix and a dev-server restart, retried via
     `POST /api/publish/jobs/{id}/retry`: succeeded.** Job went
     `queued → uploading → draft` in ~25s.
     `platform_upload_id: "v_inbox_file~v2.7702405277987014669"`, `status:
     "draft"`, note: *"Sent to your TikTok inbox as a private draft — open the
     TikTok app to add a caption and post it (unaudited apps can't post
     directly)."* Confirmed in the UI too, on the Publishing Queue page
     (`#/publish`) — same status and note, never shown as "published".
3. **Not yet done**: nobody has opened the TikTok app itself to confirm the
   draft is actually sitting in the inbox (would be good to do once, as a
   final sanity check — everything on ClipForge's side says it worked).

If the callback fails again in a future session, check the ngrok terminal's
request log and `backend/logs/clipforge.log` for the actual error — the
`/api/publish/tiktok/callback` route redirects errors to
`#/settings/publish-err/<code>` in the UI too.

## Gotchas (keep reading before you hit these)

- **#1 (Windows): AppData isn't shared with Claude's sandbox.** Always start
  servers/setup via `explorer.exe <script>.cmd`, never a plain tool-call
  process.
- **#2: the backend does not hot-reload.** Restart after *any* backend change
  — code or `.env` — before believing a fix is live.
- **#3:** `CLIPFORGE_TRANSCRIBER=sidecar` reads sidecar files instead of
  running Whisper (how tests work without a model). Default is
  `faster-whisper`.
- **#4:** `pkill -f` / `pgrep -f` in a Bash tool call match the tool's own
  shell command line. Use PowerShell `Get-CimInstance Win32_Process` +
  `Stop-Process -Id` by PID instead.
- **#5:** this shell's PATH lacks ffmpeg by default — prepend
  `AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin` for a
  tool-call `pytest` run.
- **#6:** ClipForge's sync hook is `repo-sync.mjs` on purpose (not
  `sync-check.mjs`, which MCC's installer would overwrite).
- **#7:** schema changes: `alembic upgrade head` on a throwaway db, then
  `revision --autogenerate`, then rename the generated file to `000N_name.py`.
- **#8 (new):** Windows Application Control blocks freshly-downloaded/updated
  `.exe`s with "Malicious binary reputation" even for legitimate dev tools,
  even run directly in the user's own terminal. Winget-installed and
  Microsoft-Store-installed binaries are trusted; `ngrok update` and raw
  curl-downloaded zips are not. Prefer the Store or winget's *original*
  install over any self-update mechanism for a CLI tool that hits this.
- **#9 (new):** TikTok's Production and Sandbox app versions have **separate**
  Client key/secret pairs, separate product/scope/redirect-URI configuration,
  and separate credentials entirely. Changes made on one tab (Production vs
  Sandbox) do not carry to the other, "Clone from Production" at sandbox
  creation time only seeds *some* fields. Always double check which one's
  credentials are actually in `.env`.

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
- App icon used for the TikTok app listing is a placeholder (a generated
  green "C" monogram, not a real brand asset) — fine for Sandbox, would need
  a real one before ever submitting for TikTok review.
- TikTok ToS/Privacy URLs point at the GitHub repo as a placeholder — again
  fine because this app is never being submitted for public review, only
  used in Sandbox mode against the developer's own account.

## Not verified yet / known limits

1. **TikTok draft not visually confirmed in the TikTok app itself** — every
   signal on ClipForge's/TikTok API's side says the upload succeeded, but
   nobody has opened the TikTok app to look at the inbox yet.
2. **TikTok display name shows the raw `open_id`, not a real name** — the
   `/v2/user/info/` fetch was added after this account connected; only takes
   effect on the next connect/reconnect (see "Uncommitted work").
3. **No real upload has been attempted on YouTube** — no OAuth client
   registered yet at all.
4. **YouTube has no registered OAuth client yet** — needs a Google Cloud
   Console project + OAuth client (README "Connecting YouTube"); the user
   hasn't done this.
5. **Scheduled publishing isn't honoured.** `PublishJob.scheduled_for` exists
   on the model/API; neither adapter reads it — both publish immediately.
6. **Clip ranking is a heuristic baseline (`heuristic-v1`).** No semantic
   understanding — the UI and this file both say so on purpose.
7. No speaker detection; facecam/split-screen regions are set by hand.
8. SRT/VTT imports have estimated word timings (drift within a line).
9. **No committed UI test** beyond manual browser-pane verification.
10. TikTok Production app is half-configured and abandoned — either finish it
    properly later (real ToS/Privacy/icon, submit for Content Posting API
    audit to unlock Direct Post) or delete it to avoid confusion.
11. Only one TikTok chunking path has actually been exercised against the real
    API (the ≤64 MiB single-chunk case, since the test render was ~50 MB).
    The multi-chunk branch of `_chunk_plan()` is covered by a unit test but
    not by a real upload — worth doing once with a longer/larger render.

## What's next

1. **Open the TikTok app and confirm the draft is really there** — quick
   sanity check now that every API signal says it worked.
2. **YouTube**: register a Google Cloud OAuth client (README "Connecting
   YouTube"), put the credentials in `.env`, restart, connect, test a real
   upload (YouTube needs no tunnel — loopback redirects work natively there).
3. **Milestone 2** (`docs/implementation-plan.md`): local semantic re-ranker
   via Ollama, a hand-labelled real-video eval set, speaker turns, moving face
   tracking, a timeline editor, the Tauri shell, a committed Playwright test.
4. **Milestone 3 — Original Content Studio** (not started, large, deferred).
5. **Milestone 4 — Campaign Center** (not started, large, deferred).
