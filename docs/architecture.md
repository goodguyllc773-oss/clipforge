# ClipForge — Architecture

ClipForge is a local-first clipping tool: import a long video, transcribe it on the
local machine, find coherent 30–60 s moments, render vertical captioned clips, and
review them before anything leaves the machine.

This document describes what is **built**. Planned-but-unbuilt pieces are marked
_(planned)_ and live in `implementation-plan.md`.

## Process layout

```
┌──────────────────────────┐   HTTP (127.0.0.1 only)   ┌───────────────────────────────┐
│ frontend/ (React + Vite) │ ───────────────────────▶ │ backend/ FastAPI  (clipforge) │
│  library · import · jobs │   /api/*  JSON + video   │  api/  routes, schemas         │
│  review · settings       │ ◀─────────────────────── │  jobs/ durable worker (thread) │
└──────────────────────────┘                          │        ↳ pipeline handlers     │
       (Tauri 2 shell later wraps                     └───────────────┬───────────────┘
        the same build — planned)                                     │ subprocess
                                                      ffprobe / ffmpeg│ faster-whisper (in-proc)
                                                                      ▼
                                       SQLite (WAL) + data dir (renders, cache, logs, models)
```

* One Python process (`python -m clipforge serve`) runs the API **and** the job worker
  threads. `python -m clipforge worker` can run the worker alone; a lock file makes
  sure only one worker owns a data directory at a time.
* In development the Vite dev server proxies `/api` to the backend. After
  `npm run build`, FastAPI serves `frontend/dist` itself, so the app is one command.
* The server binds to `127.0.0.1`, rejects non-local `Host` headers (DNS-rebinding
  guard) and only allows the dev origin through CORS. It lists local folders for the
  import dialog, which is why those guards matter.
* The app makes **no outbound network calls** on its own initiative. The
  user-triggered exceptions: the one-time Whisper model download, import-by-URL
  (`docs/decisions.md` D13), and publishing (D14) — each platform call happens
  only for an account the user explicitly connected (their own OAuth consent, in
  their own browser) and, for auto-publish, only for an account they explicitly
  flipped that on for. The import dialog's local-path flow and the core
  transcribe/analyse/render pipeline need none of this.

## Backend modules (`backend/clipforge/`)

| Module | Responsibility | Depends on |
|---|---|---|
| `config.py` | Static settings from env (`CLIPFORGE_*`), data-dir layout | — |
| `db/` | SQLAlchemy 2 models, engine/session, Alembic migrations | config |
| `media/probe.py` | `ffprobe` wrapper → `MediaInfo`; format validation | ffprobe |
| `media/ffmpeg.py` | Cancellable ffmpeg runner with `-progress` parsing | ffmpeg |
| `media/hwaccel.py` | Encoder discovery **with a real test encode**, CUDA detection | ffmpeg, ctranslate2 |
| `media/paths.py` | Safe file names, unique output paths, fingerprinting, dir validation | — |
| `media/download.py` | Import-by-URL: classify a URL, stream a direct file download or resolve/fetch via `yt-dlp` | ffmpeg (via yt-dlp), yt-dlp |
| `transcription/` | `Transcriber` protocol; `faster-whisper` engine (chunked, resumable); sidecar `.srt/.vtt/.json` engine | ffmpeg, faster-whisper |
| `analysis/signals.py` | Streaming loudness (RMS per 0.5 s) and scene cuts (`ffmpeg select=scene`) | ffmpeg, numpy |
| `analysis/sentences.py` | Words → sentences → paragraphs using punctuation + pauses | — |
| `analysis/candidates.py` | Sentence-aligned windows, boundary refinement, overlap suppression | sentences, scoring |
| `analysis/scoring.py` | Transparent heuristic scorer `heuristic-v1`: named features × profile weights → score + reasons | — |
| `analysis/semantic.py` | `SemanticScorer` interface for an optional model provider (none shipped) | — |
| `analysis/framing.py` | OpenCV face detection on sampled frames → crop centre | opencv, ffmpeg |
| `profiles/` | Pydantic `EditingProfile` schema + six built-in presets | — |
| `captions/` | `CaptionStyle` schema + presets; word-timed ASS generator | — |
| `render/layouts.py` | FFmpeg filtergraphs: `crop`, `fit_blur`, `split_stack`, `content_facecam` | profiles |
| `render/renderer.py` | Build the ffmpeg command, temp output, ffprobe validation, atomic move, encoder fallback | everything above |
| `jobs/queue.py` | Durable job state machine in SQLite (enqueue, claim, lease, cancel, retry, recover) | db |
| `jobs/worker.py` | Lane-based worker threads, heartbeats, cancellation plumbing | queue |
| `jobs/handlers.py` | `transcribe`, `analyze`, `render`, `import_url`, `publish` pipeline steps | all engine modules |
| `publishing/` | `PlatformAdapter` interface; real YouTube (OAuth + resumable upload) and TikTok (OAuth + inbox/draft) adapters; `credentials.py` wraps the OS keychain | yt-dlp-style stdlib HTTP, `google-api-python-client`, `keyring` |
| `services.py` | Use-cases shared by API and CLI (import media, review transitions, export, publish) | db, engine |
| `api/` | FastAPI app, routes, response schemas | services |
| `cli.py` | `serve`, `worker`, `doctor`, `migrate` | all |

The engine modules (`media`, `transcription`, `analysis`, `captions`, `render`,
`profiles`) never import `api` or `jobs`. They take plain dataclasses / pydantic
models and return them, which keeps the processing engine reusable by a future
remote worker or cloud control plane without dragging the web layer along.

## Pipeline

```
import (sync, in API request)         transcribe job (lane "transcribe")     analyze job (lane "media")           render job (lane "media")
──────────────────────────────        ─────────────────────────────────      ─────────────────────────────        ──────────────────────────────
validate path → ffprobe → reject      split audio into ~10-min chunks at     loudness curve (streamed PCM)        cut [start,end] with input -ss
unsupported/corrupt → fingerprint     silences; per chunk: ffmpeg → 16 kHz   scene cuts (if profile wants)        layout filtergraph (+face crop)
(size + sha256 head/tail) → row       PCM pipe → faster-whisper(word ts)     words → sentences → paragraphs       word-timed ASS captions → ass=
→ enqueue transcribe (or skip if      → commit chunk segments. Resume =      sentence-aligned windows             encode (NVENC/QSV/AMF if tested
no audio) → analyze                   skip committed chunks.                 heuristic-v1 scores + reasons        OK, else libx264) → .part.mp4
                                                                             overlap suppression → candidates     ffprobe validate → atomic rename
```

Every stage is idempotent: re-running `transcribe` resumes from committed chunks (or
starts a new transcript when the engine/model changed); re-running `analyze` replaces
only **pending** candidates and keeps anything the user approved or rejected;
`render` always writes a new versioned file and never overwrites an earlier render.

### Import by URL

An alternative to the local-path import above, for when the user pastes a link
instead of browsing to a file. Asynchronous (a download can be slow or large),
so it's its own job kind, in its own lane so it never blocks transcription or
rendering:

```
POST /api/projects/{id}/media/import-url {url}        import_url job (lane "download")
────────────────────────────────────────              ─────────────────────────────────
validate URL (http/https, non-empty host)   →          classify: direct file URL, or a page
enqueue import_url (dedupe by project+url)              needing yt-dlp (media/download.py)
                                                         → stream-download to
                                                           <data dir>/downloads/<job_id>/
                                                         → probe → fingerprint → create
                                                           source_media row (source_url set)
                                                           → same enqueue as local import
                                                           (transcribe/analyze)
                                                         on any failure: delete the partial
                                                         download, fail the job with a code
```

The downloaded file becomes that media's `original_path`, exactly like a
locally-browsed file from here on — same probing, fingerprint-based de-dup,
transcription, analysis and rendering path. See `docs/decisions.md` D13 for why
this is a deliberate, narrow exception to "no outbound network calls," and
what's still true (no scraping, no scheduled or automatic fetching — only the
one link the user pasted, fetched once).

## Data model

All IDs are UUID4 strings (stable across machines, safe to sync later).

```
projects ─┬─< source_media ─┬─< transcripts ─┬─< transcript_chunks   (resume bookkeeping)
          │                 │                └─< transcript_segments (text + words JSON)
          │                 ├─< analysis_runs ──< candidate_clips ──< rendered_clips ──< exports
          │                 └─(signals cached as files under cache/<media_id>/)
          └ output_dir override
editing_profiles  (built-ins seeded from code; user copies allowed)
caption_presets   (built-ins seeded from code; user presets allowed)
jobs ──< job_events                    app_settings (key → JSON)
```

Key fields:

* **source_media**: `original_path` (last known absolute path), `file_name`,
  `file_size`, `fingerprint`, probe results (`duration_s`, `width`, `height`, `fps`,
  codecs, `has_audio`), `status` (`ready` · `missing` · `invalid`), `source_url`
  (set only when the media came in through import-by-URL; provenance, never
  re-fetched automatically).
* **candidate_clips**: `start_s`, `end_s`, `original_start_s/end_s` (what the analyser
  proposed), `transcript_text`, `score`, `score_breakdown` (feature → value/weight),
  `reasons` (human sentences with evidence), `warnings`, `profile_key`,
  `caption_preset_key`, `layout_overrides`, `caption_overrides`, `review_status`
  (`pending` · `approved` · `rejected`), `post_title`, `post_hashtags`.
* **rendered_clips**: `version`, `status`, `rel_path` (relative to the data dir),
  `settings` (exact ffmpeg argv, encoder, resolution, resolved layout + caption style),
  `validation` (ffprobe facts checked), `error`.
* **jobs**: `kind`, `lane`, `status` (`queued` · `running` · `completed` · `failed` ·
  `cancelled`), `stage`, `progress`, `payload`, `result`, `error_code`, `error`,
  `attempts`, `max_attempts`, `dedupe_key`, `cancel_requested`, `claim_token`,
  `worker_id`, `lease_expires_at`, `run_after`, timestamps. A **partial unique index**
  on `dedupe_key WHERE status IN ('queued','running')` makes duplicate active jobs
  impossible at the database level.

## Job state machine

```
            enqueue (dedupe: returns existing active job)
                 │
                 ▼
   ┌──────── queued ◀──────────── retry (manual, or automatic with backoff
   │  cancel     │ claim (atomic       │   for retryable errors while
   │             ▼  UPDATE…WHERE)      │   attempts < max_attempts)
   │          running ── error ────────┤
   │   cancel ╱  │  ╲ lease expired    │
   ▼         ╱   │   ╲ (crash/restart) → queued (or failed if attempts exhausted)
cancelled ◀╯     │
                 ▼
             completed           failed (terminal until a manual retry)
```

* **Claim** is a single `UPDATE jobs … WHERE id = (SELECT … LIMIT 1) AND status='queued'`
  that stamps a random `claim_token`; SQLite executes it atomically, so two workers can
  never run the same job.
* **Leases**: a running job's worker heartbeats every few seconds. On startup the
  worker takes the data-dir lock; any `running` job it does not own is orphaned
  (previous process died) and is re-queued immediately. A job interrupted
  `max_attempts` times is failed with `INTERRUPTED`.
* **Cancellation**: queued jobs cancel immediately; running jobs see
  `cancel_requested` at the next progress callback, the ffmpeg subprocess is killed,
  temp files are removed and the job ends `cancelled`.
* **Errors** are `JobError(code, message, retryable, hint)`. Non-retryable codes
  (`SOURCE_MISSING`, `INVALID_MEDIA`, `NO_TRANSCRIPT`…) fail at once with an actionable
  message; retryable ones back off `5 s × 2^(attempt−1)` (capped at 5 min). A graceful
  shutdown re-queues running jobs without spending an attempt.
* A job only becomes `completed` when its handler returns; the render handler only
  returns after ffprobe validation passes.

## Files on disk

```
<data dir>  (Windows: %LOCALAPPDATA%\ClipForge, Linux: ~/.local/share/clipforge,
             override: CLIPFORGE_DATA_DIR)
├── clipforge.db            SQLite (WAL)
├── worker.lock             single-worker lock
├── logs/clipforge.log      structured (JSON-lines) log, rotated
├── models/                 faster-whisper download_root
├── downloads/<job_id>/     import-by-URL: the fetched file, kept permanently as
│                           that media's source (not cleaned up like cache/tmp)
├── cache/<media_id>/       loudness.json, scenes-<threshold>.json
├── renders/<project-slug>-<id8>/<clip>_v<n>.mp4   (+ .tmp/ for in-progress output)
└── (exports go wherever the user chooses)
```

* Source footage is **never** copied, modified or deleted. Its absolute path is stored
  as a _last known location_ next to a content fingerprint. If the file disappears the
  media row flips to `missing`, jobs fail with `SOURCE_MISSING`, and
  `POST /api/media/{id}/relink` accepts a new path after verifying the fingerprint.
* Everything ClipForge produces is stored **relative to the data dir**, so the data
  dir can be moved or synced as a unit.

## Editing profiles

A profile is data, validated by `profiles/schema.py`:

```
EditingProfile
  key, name, description, content_type
  duration:      min_s · target_s · max_s · hard_max_s
  segmentation:  sentence_pause_s · paragraph_pause_s · use_scene_cuts · scene_threshold
  scoring:       weights{feature → float}  (features listed in analysis/scoring.py)
  layout:        kind (crop | fit_blur | split_stack | content_facecam) + params
  framing:       face_tracking (bool) · samples
  captions:      preset_key · position (lower | middle | upper | seam)
  max_candidates, min_gap_s
```

Profiles change behaviour in three places: **which windows score well**
(weights — e.g. reaction weights audio energy and dynamics, podcast weights
self-contained Q&A), **how windows are cut** (duration, pauses, scene snapping), and
**how the clip is framed and captioned** (layout + caption position).

## Scoring (heuristic-v1) — what it is and is not

`heuristic-v1` is a transparent baseline, **not** semantic understanding. Each
candidate gets named features in `[0,1]`:

`hook` (opens with a question / hook phrase, not a dangling connective) ·
`completeness` (ends on terminal punctuation followed by a pause) ·
`self_contained` (starts after a pause / paragraph break, no back-reference) ·
`energy` and `dynamics` (loudness vs. this video's median, and its variance) ·
`pace` (words per second in a comfortable band, little dead air) ·
`emphasis` (exclamations, laughter tokens, intensifiers) ·
`qa` (a question followed by a substantial answer) ·
`focus` (content words repeated within the window) ·
`duration_fit` · `scene_alignment` · `visual_activity`.

Score = weighted mean × 100. The top contributing features become the clip's
`reasons`, each with concrete evidence (the quoted opening line, the measured dB, the
pause length). It cannot tell whether something is actually funny or insightful. The
`SemanticScorer` interface (`analysis/semantic.py`) is where a local LLM (e.g. Ollama)
or a cloud provider plugs in later to re-rank the top-N; nothing implements it yet.

## Captions

Word timings → caption **pages** (≤ 2 lines, ≤ N chars per line, breaking
preferentially at sentence ends, then commas) → one ASS `Dialogue` event per word
interval showing the whole page with the active word recoloured and popped
(`\t` scale animation). Missing word timestamps are interpolated across the segment by
character length; overlapping or zero-length words are made monotonic with a minimum
duration. Positions keep inside platform-safe margins (top 12 %, bottom 22 %,
sides 8 %), and a caption block that would overlap a detected face moves to the other
safe zone. The ASS file is written next to the temp output and referenced by a
relative name so Windows drive-letter escaping never reaches the filter string.

## API (summary)

```
GET  /api/health · /api/system/diagnostics
GET|POST /api/projects            GET|PATCH /api/projects/{id}
POST /api/projects/{id}/media     {path, profile_key?, auto_process?}
POST /api/projects/{id}/media/import-url  {url, profile_key?, auto_process?}  (202, async)
GET  /api/media/{id} · /api/media/{id}/transcript · /api/media/{id}/candidates
GET  /api/media/{id}/source (Range) · /api/media/{id}/frame?t=
POST /api/media/{id}/transcribe · /analyze {profile_key?} · /relink {path, force?}
GET|PATCH /api/candidates/{id}    {start_s,end_s,snap,profile_key,caption_preset_key,…}
POST /api/candidates/{id}/review  {action: approve|reject|reset}
POST /api/candidates/{id}/render  GET /api/candidates/{id}/renders
GET  /api/renders/{id}/video (Range) · POST /api/renders/{id}/export {folder}
GET  /api/jobs?status=&media_id= · GET /api/jobs/{id} · POST /api/jobs/{id}/cancel|retry
GET  /api/profiles · /api/caption-presets (POST to add a custom preset)
GET|PUT /api/settings · GET /api/fs/list?path=

GET  /api/publish/platforms                      {youtube: {configured}, tiktok: {configured}}
GET  /api/publish/{platform}/connect             → {authorize_url}; GET .../callback (OAuth redirect target)
GET  /api/publish/accounts · PATCH .../{id} {auto_publish} · POST .../{id}/disconnect
POST /api/renders/{id}/publish  {account_id, title?, description?, tags?, privacy?, scheduled_for?}  (202, async)
GET  /api/publish/jobs?status=&platform= · GET .../{id} · POST .../{id}/cancel|retry
```

## Future SaaS seams (not built)

* **Auth / entitlements / usage**: a thin `entitlements` check would sit in
  `services.py` before enqueueing jobs; the engine never sees accounts.
* **Cloud control plane / remote workers**: the job table already carries `lane`,
  `worker_id` and leases; a remote worker would claim through an API instead of
  SQLite. Raw footage stays local unless the user opts into remote processing.
* **Source connectors**: a `SourceConnector` interface (list new items from a
  creator-authorised source, fetch with the creator's own credentials) feeding the same
  `import` use-case. No scraping. Import-by-URL (above) is not this: it fetches exactly
  the one link the user pastes, on demand, never lists or polls a source on its own.
* **Original Content Studio**: script generation/editing, narration (TTS)
  adapters, a background-visual track system, and series continuity, reusing
  this same render/caption engine for original (non-clipped) short-form video.
  Not started.
* **Campaign Center**: paid-clipping campaign records, campaign-aware clip
  requirements, submission tracking, and estimated-vs-confirmed earnings
  analytics. Not started.

**Publishing is now built** — see `docs/decisions.md` D14, the "Publishing"
module row above, and the `/api/publish/*` routes. YouTube is real
(OAuth 2.0 + the official resumable upload API); TikTok is real OAuth + the
inbox/draft upload (what an unaudited app is allowed to do) with Direct Post
left as a documented `PlatformNotSupported` until the app has TikTok's review
approval.
