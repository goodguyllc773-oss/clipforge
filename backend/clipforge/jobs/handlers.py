"""Pipeline job handlers. Each is idempotent and safe to retry:

* ``transcribe`` resumes from committed chunks; a completed transcript with the
  same engine+model is reused unless ``force``.
* ``analyze`` replaces only untouched pending candidates.
* ``render`` reuses its own half-made row on retry and always writes a new file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import delete, func, select

from ..analysis.candidates import generate_candidates, overlap_fraction
from ..analysis.framing import summarize_faces
from ..analysis.signals import Loudness, cache_json, compute_loudness, detect_scenes, plan_chunks
from ..db.models import (
    AnalysisRun, CandidateClip, Project, PublishAccount, PublishJob, RenderedClip, SourceMedia,
    Transcript, TranscriptChunk, TranscriptSegment, utcnow,
)
from ..errors import ClipForgeError, JobCancelled, NotFound, PlatformNotSupported, ReauthRequired
from ..media import download
from ..media.paths import format_ts, slugify, to_relative
from ..publishing import adapter as publish_adapter
from ..publishing import credentials as publish_credentials
from ..publishing.base import PublishRequest
from ..render.renderer import RenderRequest, render_clip
from .. import services
from . import queue
from .worker import JobContext


def _media(ctx: JobContext, media_id: str) -> SourceMedia:
    with ctx.app.session() as s:
        m = s.get(SourceMedia, media_id)
        if m is None:
            raise NotFound("Media was deleted.", code="MEDIA_GONE")
        s.expunge(m)
        return m


def _loudness(ctx: JobContext, media: SourceMedia, src: Path, span: tuple[float, float]) -> Loudness | None:
    if not media.has_audio:
        return None
    lo, hi = span

    def compute():
        return compute_loudness(str(src), media.duration_s, ffmpeg=ctx.app.settings.ffmpeg,
                                on_progress=lambda f: ctx.progress(lo + (hi - lo) * f)).to_json()
    data = cache_json(ctx.app.settings.cache_dir / media.id / "loudness.json", compute)
    return Loudness.from_json(data)


# --------------------------------------------------------------------------- transcribe
def handle_transcribe(ctx: JobContext) -> dict:
    media_id = ctx.payload["media_id"]
    media = _media(ctx, media_id)
    src = services.check_source(ctx.app, media_id)
    if not media.has_audio:
        return {"skipped": "no audio stream"}

    ctx.stage("Measuring audio", 0.0)
    loud = _loudness(ctx, media, src, (0.0, 0.05))
    transcriber = ctx.app.transcriber_factory(ctx.app, media)
    desc = transcriber.describe()
    try:
        with ctx.app.session() as s:
            existing = s.scalar(select(Transcript).where(
                Transcript.media_id == media_id, Transcript.engine == desc["engine"],
                Transcript.model == str(desc.get("model", ""))).order_by(Transcript.created_at.desc()).limit(1))
            if existing and existing.status == "complete" and not ctx.payload.get("force"):
                tid, reused = existing.id, True
            elif existing and existing.status == "partial":
                tid, reused = existing.id, False
                ctx.event(f"Resuming transcript ({existing.chunks_done}/{existing.chunks_total} chunks done).")
            else:
                chunks = plan_chunks(media.duration_s, ctx.app.settings.transcribe_chunk_s, loud)
                t = Transcript(media_id=media_id, engine=desc["engine"], model=str(desc.get("model", "")),
                               device=str(desc.get("device", "")), settings=desc, chunks_total=len(chunks))
                s.add(t)
                s.flush()
                for i, (a, b) in enumerate(chunks):
                    s.add(TranscriptChunk(transcript_id=t.id, index=i, start_s=a, end_s=b))
                tid, reused = t.id, False
        if reused:
            ctx.event("A complete transcript with the same engine/model already exists; reusing it.")
        else:
            _run_chunks(ctx, transcriber, tid, src, media)
    finally:
        transcriber.release()

    if ctx.payload.get("auto_analyze"):
        services.request_analysis(ctx.app, media_id)
    with ctx.app.session() as s:
        t = s.get(Transcript, tid)
        n_seg = s.scalar(select(func.count()).select_from(TranscriptSegment)
                         .where(TranscriptSegment.transcript_id == tid))
        return {"transcript_id": tid, "segments": n_seg, "language": t.language, "engine": t.engine,
                "model": t.model, "device": t.device, "reused": reused}


def _run_chunks(ctx: JobContext, transcriber, tid: str, src: Path, media: SourceMedia) -> None:
    with ctx.app.session() as s:
        chunks = [(c.index, c.start_s, c.end_s) for c in
                  s.scalars(select(TranscriptChunk).where(TranscriptChunk.transcript_id == tid,
                                                          TranscriptChunk.status == "pending")
                            .order_by(TranscriptChunk.index)).all()]
        total = s.get(Transcript, tid).chunks_total
    dur = max(1.0, media.duration_s)
    for idx, a, b in chunks:
        ctx.stage(f"Transcribing part {idx + 1}/{total}", 0.05 + 0.95 * a / dur)
        res = transcriber.transcribe_range(str(src), a, b,
                                           on_progress=lambda t: ctx.progress(0.05 + 0.95 * min(t, b) / dur))
        for w in res.warnings:
            ctx.event(w, "warning")
        ctx.check()
        with ctx.app.session() as s:
            # Same transaction: segments + chunk status, so a crash never half-commits.
            s.execute(delete(TranscriptSegment).where(TranscriptSegment.transcript_id == tid,
                                                      TranscriptSegment.chunk_index == idx))
            for k, seg in enumerate(res.segments):
                s.add(TranscriptSegment(transcript_id=tid, chunk_index=idx, idx=k, start_s=seg.start,
                                        end_s=seg.end, text=seg.text, words=[w.to_json() for w in seg.words],
                                        avg_logprob=seg.avg_logprob, no_speech_prob=seg.no_speech_prob))
            chunk = s.scalar(select(TranscriptChunk).where(TranscriptChunk.transcript_id == tid,
                                                          TranscriptChunk.index == idx))
            chunk.status = "done"
            t = s.get(Transcript, tid)
            t.chunks_done = t.chunks_done + 1
            t.language = t.language or res.language
            if res.device:
                t.device = res.device
    with ctx.app.session() as s:
        t = s.get(Transcript, tid)
        t.status, t.completed_at = "complete", utcnow()
        t.word_timestamps = not any(
            w.get("est") for seg in t.segments for w in seg.words)


# --------------------------------------------------------------------------- analyze
def handle_analyze(ctx: JobContext) -> dict:
    media_id = ctx.payload["media_id"]
    media = _media(ctx, media_id)
    src = services.check_source(ctx.app, media_id)
    profile_key = ctx.payload.get("profile_key") or media.profile_key
    profile = ctx.app.profile(profile_key)

    with ctx.app.session() as s:
        t = services.latest_transcript(s, media_id)
        if media.has_audio and t is None:
            raise ClipForgeError("No completed transcript yet.", code="NO_TRANSCRIPT", retryable=False,
                                 hint="Run transcription first (it starts automatically on import).")
        words = services.transcript_words(t)
        tid = t.id if t else None

    ctx.stage("Measuring audio", 0.02)
    loud = _loudness(ctx, media, src, (0.02, 0.3))
    cuts: list[float] = []
    if profile.segmentation.use_scene_cuts or not words:
        ctx.stage("Detecting scene changes", 0.3)
        thr = profile.segmentation.scene_threshold
        data = cache_json(ctx.app.settings.cache_dir / media_id / f"scenes-{thr:.2f}.json",
                          lambda: detect_scenes(str(src), media.duration_s, ffmpeg=ctx.app.settings.ffmpeg,
                                                threshold=thr,
                                                on_progress=lambda f: ctx.progress(0.3 + 0.55 * f)))
        cuts = [c["t"] for c in data]

    ctx.stage("Finding clip candidates", 0.87)
    cands, stats = generate_candidates(words, media_duration=media.duration_s, profile=profile,
                                       loudness=loud, scene_cuts=cuts)
    ctx.check()
    with ctx.app.session() as s:
        run = AnalysisRun(media_id=media_id, transcript_id=tid, profile_key=profile.key,
                          scorer=stats["scorer"], params=profile.model_dump(mode="json"), stats=stats)
        s.add(run)
        s.flush()
        existing = s.scalars(select(CandidateClip).where(CandidateClip.media_id == media_id)).all()
        kept, removed = [], 0
        for c in existing:
            touched = (c.review_status != "pending" or c.renders or c.layout_overrides or c.caption_overrides
                       or abs(c.start_s - c.original_start_s) > 1e-3 or abs(c.end_s - c.original_end_s) > 1e-3)
            if touched:
                kept.append(c)
            else:
                s.delete(c)
                removed += 1
        added = 0
        caption_key = profile.captions.preset_key
        for rank, c in enumerate(cands, start=1):
            if any(overlap_fraction((c.start, c.end), (k.start_s, k.end_s)) > profile.max_overlap for k in kept):
                continue
            s.add(CandidateClip(
                media_id=media_id, analysis_run_id=run.id, rank=rank, start_s=c.start, end_s=c.end,
                original_start_s=c.start, original_end_s=c.end, transcript_text=c.text, score=c.score,
                score_breakdown=c.features, reasons=c.reasons, warnings=c.warnings, profile_key=profile.key,
                caption_preset_key=caption_key, post_title=c.post_title, post_hashtags=c.hashtags))
            added += 1
        run.stats = {**stats, "added": added, "kept": len(kept), "replaced": removed}
        run_id = run.id
    return {"analysis_run_id": run_id, "added": added, "kept": len(kept), "replaced": removed,
            "scorer": stats["scorer"], "fallback": stats.get("fallback")}


# --------------------------------------------------------------------------- render
def handle_render(ctx: JobContext) -> dict:
    cid = ctx.payload["candidate_id"]
    app = ctx.app
    prefs = app.prefs()
    with app.session() as s:
        c = s.get(CandidateClip, cid)
        if c is None:
            raise NotFound("Clip was deleted.", code="CANDIDATE_GONE")
        media, project = c.media, c.media.project
        profile = services.effective_profile(app, s, c)
        style = services.effective_caption_style(app, s, c)
        words = services.transcript_words(services.latest_transcript(s, media.id), c.start_s - 1, c.end_s + 1)
        row = s.scalar(select(RenderedClip).where(RenderedClip.candidate_id == cid,
                                                  RenderedClip.job_id == ctx.job_id))
        if row is None:  # first attempt; a retry reuses its own row
            version = (s.scalar(select(func.max(RenderedClip.version)).where(RenderedClip.candidate_id == cid)) or 0) + 1
            row = RenderedClip(candidate_id=cid, job_id=ctx.job_id, version=version, start_s=c.start_s,
                               end_s=c.end_s, profile_key=c.profile_key, caption_preset_key=c.caption_preset_key)
            s.add(row)
        row.status, row.error = "rendering", None
        row.start_s, row.end_s = c.start_s, c.end_s
        s.flush()
        render_id, version = row.id, row.version
        out_dir = services.render_output_dir(app, project)
        name = (f"{slugify(Path(media.file_name).stem, 40, 'clip')}_{c.rank:02d}_"
                f"{format_ts(c.start_s)}_v{version}.mp4")
        start, end = c.start_s, c.end_s
        m_info = (media.id, media.width, media.height, media.fps, media.has_audio)

    try:
        src = services.check_source(app, m_info[0])
        face = None
        if profile.layout.kind == "crop" and profile.framing.face_tracking:
            ctx.stage("Finding faces", 0.02)
            face = summarize_faces(str(src), start, end, m_info[1], m_info[2],
                                   samples=profile.framing.samples, ffmpeg=app.settings.ffmpeg)
            ctx.event("Face found; crop follows it." if face else "No face found; using the centre crop.")
        ctx.stage("Checking encoders", 0.05)
        hw = app.hardware()
        ctx.stage("Rendering", 0.06)
        req = RenderRequest(
            source=str(src), start_s=start, end_s=end, src_w=m_info[1], src_h=m_info[2], src_fps=m_info[3],
            has_audio=m_info[4], profile=profile, caption_style=style, words=words, output_dir=out_dir,
            tmp_root=out_dir / ".tmp", final_name=name, out_w=int(prefs["output_width"]),
            out_h=int(prefs["output_height"]), encoder_preference=prefs["encoder_preference"], face=face,
            deep_validate=bool(prefs["deep_validate"]))
        out = render_clip(req, hw=hw, ffmpeg=app.settings.ffmpeg, ffprobe=app.settings.ffprobe,
                          on_progress=lambda f: ctx.progress(0.06 + 0.94 * f))
    except Exception as e:  # record on the render row, then let the worker classify it
        with app.session() as s:
            r = s.get(RenderedClip, render_id)
            if r is not None:
                r.status = "cancelled" if isinstance(e, JobCancelled) else "failed"
                r.error = getattr(e, "message", None) or str(e) or type(e).__name__
        raise

    for w in out.warnings:
        ctx.event(w, "warning")
    with app.session() as s:
        r = s.get(RenderedClip, render_id)
        r.status, r.rel_path = "completed", to_relative(out.path, app.settings.data_dir)
        r.encoder, r.width, r.height = out.encoder, req.out_w, req.out_h
        r.duration_s, r.file_size, r.settings, r.validation = out.duration_s, out.file_size, out.settings, out.validation
    return {"render_id": render_id, "version": version, "encoder": out.encoder, "file": out.path.name,
            "duration_s": out.duration_s, "warnings": out.warnings}


# --------------------------------------------------------------------------- import by URL
def handle_import_url(ctx: JobContext) -> dict:
    """Download a pasted link, then feed it into the same ``import_media`` use-case
    as a locally-browsed file. See ``media/download.py`` and ``docs/decisions.md`` D13."""
    app = ctx.app
    project_id = ctx.payload["project_id"]
    url = ctx.payload["url"]
    profile_key = ctx.payload.get("profile_key")
    auto_process = ctx.payload.get("auto_process", True)

    with app.session() as s:
        if s.get(Project, project_id) is None:
            raise NotFound("Project was deleted.", code="PROJECT_GONE")

    dest_dir = app.settings.downloads_dir / ctx.job_id
    ctx.stage("Resolving link", 0.0)
    downloading = False

    def on_progress(frac: float | None, done_bytes: int) -> None:
        nonlocal downloading
        if frac is not None:
            value = 0.05 + 0.85 * frac
        else:
            # Unknown total: creep toward (but never reach) the "Importing"
            # handoff point as bytes come in, so the bar visibly moves instead
            # of sitting frozen for the whole download.
            mb = done_bytes / (1 << 20)
            value = 0.05 + 0.80 * (1 - 1 / (1 + mb / 200))
        if not downloading:
            downloading = True
            ctx.stage("Downloading", value)  # one forced flush for the label switch
        else:
            ctx.progress(value)  # throttled — safe to call on every hook tick

    try:
        path, kind = download.fetch(url, dest_dir, on_progress=on_progress, cancel_check=ctx.check)
        ctx.event(f"Downloaded via {'direct link' if kind == 'direct' else 'yt-dlp'}.")
        ctx.stage("Importing", 0.92)
        m, jobs = services.import_media(app, project_id, str(path), profile_key=profile_key,
                                        auto_process=auto_process, source_url=url)
    except BaseException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return {"media_id": m.id, "job_ids": jobs, "source_url": url, "kind": kind}


# --------------------------------------------------------------------------- publishing
def _fail_publish_job(app, publish_job_id: str, *, account_id: str | None, needs_reauth: bool,
                      code: str, message: str) -> None:
    with app.session() as s:
        pj = s.get(PublishJob, publish_job_id)
        if pj is None:
            return
        pj.status = "needs_action" if needs_reauth else "failed"
        pj.error_code, pj.error = code, message
        pj.retry_count += 1
        if needs_reauth and account_id:
            acc = s.get(PublishAccount, account_id)
            if acc is not None:
                acc.status = "needs_reauth"


def handle_publish(ctx: JobContext) -> dict:
    """Upload one rendered clip to one connected platform account. A thin
    shell around the platform adapter (``publishing/``) — this handler only
    owns job/account bookkeeping, never a platform's wire format."""
    app = ctx.app
    publish_job_id = ctx.payload["publish_job_id"]

    with app.session() as s:
        pj = s.get(PublishJob, publish_job_id)
        if pj is None:
            raise NotFound("Publish job was deleted.")
        if pj.account_id is None:
            raise NotFound("The account for this publish job was removed.", code="ACCOUNT_GONE")
        account = s.get(PublishAccount, pj.account_id)
        if account is None or account.status != "connected":
            raise NotFound("The account for this publish job isn't connected any more.",
                           code="ACCOUNT_NOT_CONNECTED")
        render = s.get(RenderedClip, pj.render_id)
        if render is None or render.status != "completed":
            raise NotFound("The render for this publish job is gone.")
        file_path = services.render_file(app, render)
        if not file_path.is_file():
            raise NotFound("The rendered file is missing from the ClipForge data folder — render it again.")
        req = PublishRequest(file_path=file_path, title=pj.title, description=pj.description,
                             tags=pj.tags or [], privacy=pj.privacy, scheduled_for=pj.scheduled_for)
        platform, account_id, credential_key = pj.platform, account.id, account.credential_key
        pj.status, pj.last_operation = "uploading", "Starting upload."

    ad = publish_adapter(platform)
    token_data = publish_credentials.load_token(credential_key)
    if token_data is None:
        _fail_publish_job(app, publish_job_id, account_id=account_id, needs_reauth=True,
                          code="REAUTH_REQUIRED", message="No stored credentials for this account.")
        raise ReauthRequired("This account's credentials are missing.",
                             hint="Reconnect it in Settings → Integrations.")
    try:
        token_data = ad.refresh(app.settings, token_data)
        publish_credentials.save_token(credential_key, token_data)
    except ReauthRequired as e:
        _fail_publish_job(app, publish_job_id, account_id=account_id, needs_reauth=True,
                          code=e.code, message=e.message)
        raise

    ctx.stage("Uploading", 0.05)
    try:
        result = ad.publish(token_data, req, on_progress=lambda f: ctx.progress(0.05 + 0.90 * min(f, 1.0)),
                            cancel_check=ctx.check)
    except ReauthRequired as e:
        _fail_publish_job(app, publish_job_id, account_id=account_id, needs_reauth=True,
                          code=e.code, message=e.message)
        raise
    except PlatformNotSupported as e:
        with app.session() as s:
            pj = s.get(PublishJob, publish_job_id)
            pj.status, pj.error_code, pj.error = "needs_action", e.code, e.message
        raise
    except ClipForgeError as e:
        with app.session() as s:
            pj = s.get(PublishJob, publish_job_id)
            pj.status = "failed"
            pj.error_code, pj.error, pj.retry_count = e.code, e.message, pj.retry_count + 1
        raise

    with app.session() as s:
        pj = s.get(PublishJob, publish_job_id)
        pj.status = result.status
        pj.platform_upload_id, pj.platform_post_id = result.platform_upload_id, result.platform_post_id
        pj.platform_post_url, pj.last_operation = result.platform_post_url, result.note
        if result.status == "published":
            pj.published_at = utcnow()
        ctx.event(result.note or f"{platform}: {result.status}.")
    return {"publish_job_id": publish_job_id, "status": result.status,
           "platform_post_url": result.platform_post_url}


HANDLERS = {"transcribe": handle_transcribe, "analyze": handle_analyze, "render": handle_render,
           "import_url": handle_import_url, "publish": handle_publish}

__all__ = ["HANDLERS", "queue"]
