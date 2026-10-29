# ClipForge — Decision log

Short records of choices that shape the codebase. Newest last.

## D1. Its own repository
ClipForge started as a `clipforge/` folder in the user's `Alfred` tools repo and was
moved (with its history, via `git subtree split`) into its own public repo,
`goodguyllc773-oss/clipforge`, on 2026-10-29 at the user's request. That matches how
the user's other projects (MCC, AmtrackAIO) are organised: own HANDOFF.md, own
CLAUDE.md, own session-sync hook.

## D2. Python engine + FastAPI, React/Vite UI, Tauri deferred
Video tooling (faster-whisper, OpenCV, numpy) is Python-native, so the engine is
Python. The UI talks to it over a localhost HTTP API, which works today in any
browser and is exactly what a Tauri 2 shell would wrap later (the shell would start
the backend as a sidecar and point a webview at it). Tauri was **not** added in
milestone 1: it needs the Rust toolchain + WebView2 setup on Windows and adds no
capability the pipeline needs yet. Native file dialogs are replaced by a server-side
folder browser (`/api/fs/list`) for now.

## D3. FFmpeg subprocesses, not Python bindings
`ffprobe`/`ffmpeg` run as subprocesses with `-progress pipe:1`. This keeps memory
bounded (no frames in Python), lets us kill a render on cancel, and makes every
render reproducible — the exact argv is stored on the render row. PyAV (pulled in
by faster-whisper) is not used directly.

## D4. ASS subtitles via libass, referenced by relative path
Word-level animated captions are ASS events rendered by ffmpeg's `ass` filter.
ffmpeg filter-string escaping of Windows paths (`C\:/...`) is notoriously fragile,
so ffmpeg runs with `cwd` set to a per-render work dir and the ASS file is
referenced as `captions.ass`.

## D5. Hardware encoders are trusted only after a test encode
`ffmpeg -encoders` listing `h264_nvenc` says nothing about drivers. Each hardware
encoder gets a 10-frame test encode; the result (with the encoder's own error line)
is cached and shown in Settings → Diagnostics. If a hardware encode still fails
mid-render we retry once with libx264 and record a warning. In this development
container NVENC/QSV are compiled in but fail (`Cannot load libcuda.so.1`), which is
exactly the case this guards against.

## D6. Chunked transcription with per-chunk commits
faster-whisper decodes its whole input into RAM (16 kHz float32 ≈ 230 MB/hour). We
feed it ~10-minute PCM chunks from an ffmpeg pipe instead, with cut points moved to
the quietest moment within ±30 s. Each chunk's segments and its `done` flag commit in
one transaction, which is what makes resume-after-crash safe. Default model `small`
with `int8_float16` on CUDA (~1 GB VRAM on the RTX 2060 Super), `int8` on CPU; the
model is released after each job so NVENC has the VRAM.

## D7. SQLite job queue instead of Celery/Redis/RQ
Single machine, single user, $0 budget: a job table with an atomic claim
(`UPDATE … WHERE status='queued'` + claim token), leases + heartbeats, a partial
unique index for dedupe, and an OS file lock for "one worker per data dir" covers
the durability needs without another service. The lane/lease/worker_id columns are
the seam for a remote worker later.

## D8. Transparent heuristic scorer first
Milestone 1 ships `heuristic-v1`: named structural features × per-profile weights,
with evidence strings. It is labelled as a baseline everywhere it's shown. A
`SemanticScorer` protocol is the plug-in point for a local LLM (Ollama is already
used by the user's Alfred app) — deliberately not built until the baseline
pipeline is solid, and to avoid pretending semantic understanding exists.

## D9. Source files: absolute path + content fingerprint
Sources stay where the user keeps them (often external drives). We store the last
known absolute path plus `v1:<size>:<sha256(first MiB + last MiB)>`. Relinking
verifies the fingerprint; everything ClipForge writes is stored relative to the data
dir so the data dir itself can move.

## D10. Sidecar transcripts as a first-class engine
Importing an existing `.srt/.vtt/.json` is a real feature (creators often have
captions already) and also gives the test suite deterministic transcripts. SRT/VTT
words are timed by interpolation and flagged `estimated`; the UI warns about it.

## D11. Silent videos still produce candidates — labelled as weak
No audio → no transcript → candidates come from scene changes (or even windows),
with a reason that says so and a warning that captions will be empty. Renders get a
silent AAC track because some platforms reject video-only uploads.

## D12. Approve auto-renders; export requires approval
Approving a clip without a completed render queues one (deduplicated). Export is
only enabled for approved clips, and always writes a new file (`name (2).mp4`) next
to a `.json` with post metadata and a `.txt` caption. Nothing is published.

## D13. Import by URL — a deliberate, scoped exception to "no outbound calls"
Until now the verified guarantee was "no outbound network calls except the
one-time Whisper model download." The user asked for a second way to import: paste
a link instead of browsing to a local file, including YouTube/TikTok/etc. links,
not just direct video URLs. That requires ClipForge to fetch something from the
network — so this is a new, narrow exception, not a reversal of the rule.

**What changed:** `POST /api/projects/{id}/media/import-url` enqueues an
`import_url` job (lane `download`, so it never blocks transcription or rendering).
The job classifies the URL (`media/download.py`): a path ending in a supported
video extension is fetched directly with a streaming HTTP download; anything else
(YouTube, TikTok, a page that embeds a player, a CDN link with no extension) is
resolved and fetched through `yt-dlp`, a new backend dependency. The downloaded
file lands under `<data dir>/downloads/<job_id>/` and becomes that media's
`original_path`, exactly like a locally-browsed file — probed, fingerprinted, and
fed into the same `import_media` use-case. The source URL is kept on the
`source_media` row (`source_url`) as provenance, never as something ClipForge
re-fetches on its own later.

**What's still true:** a network fetch happens **only** when the user pastes a URL
into the import dialog and submits it — nothing runs on a schedule, nothing crawls
a channel or playlist for more videos, and there is no general scraping. `yt-dlp`
is invoked exactly once per pasted link, for that link only. The existing
local-path import is unchanged and remains the default. Downloaded footage is
still never modified after the fact — same as footage the user points at directly.

**Why `yt-dlp` and not a hand-rolled extractor:** it is the maintained, widely used
tool for exactly this (resolving a page URL to a direct media stream across many
sites), and reimplementing per-site extraction would be both more code and more
fragile. ClipForge only uses it to fetch what the pasted URL already points to —
it doesn't browse, search or authenticate on the user's behalf.
