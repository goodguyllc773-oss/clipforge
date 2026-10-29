"""Publishing: the provider-neutral queue/state-machine, account connection,
and both adapters with the platform SDKs mocked out (no real network, no real
credentials — see docs/decisions.md D14). The one real thing tested is that
the adapters build correct requests/URLs against fake client credentials."""

from __future__ import annotations

import pytest

from clipforge import services
from clipforge.db.models import CandidateClip, Job, PublishAccount, PublishJob, RenderedClip
from clipforge.errors import AccountNotConnected, Conflict, NotFound, PlatformNotSupported, ReauthRequired
from clipforge.jobs import queue
from clipforge.publishing import ADAPTERS
from clipforge.publishing.base import AccountInfo, PublishResult


class FakeAdapter:
    """A stand-in for a real platform SDK. Each test configures ``result``
    (a PublishResult or an exception to raise) and reads back the calls it
    recorded."""
    name = "youtube"

    def __init__(self):
        self.exchange_calls: list[str] = []
        self.refresh_calls: list[dict] = []
        self.publish_calls: list[tuple] = []
        self.account_info = AccountInfo(external_account_id="chan-1", display_name="My Channel")
        self.result: PublishResult | Exception = PublishResult(
            status="uploaded", platform_upload_id="vid1", platform_post_id="vid1",
            platform_post_url="https://youtu.be/vid1", note="Uploaded.")
        self.refresh_result: dict | Exception | None = None

    def authorize_url(self, settings, redirect_uri, state):
        return f"https://fake.example/auth?redirect_uri={redirect_uri}&state={state}"

    def exchange_code(self, settings, redirect_uri, code):
        self.exchange_calls.append(code)
        return {"token": "tok", "refresh_token": "rtok"}, self.account_info

    def refresh(self, settings, token_data):
        self.refresh_calls.append(token_data)
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        return self.refresh_result if self.refresh_result is not None else {**token_data, "token": "tok2"}

    def publish(self, token_data, req, *, on_progress=None, cancel_check=None):
        self.publish_calls.append((token_data, req))
        if on_progress:
            on_progress(1.0)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def fake_adapter(monkeypatch):
    fa = FakeAdapter()
    monkeypatch.setitem(ADAPTERS, "youtube", fa)
    return fa


@pytest.fixture
def connected_account(ctx, fake_adapter) -> PublishAccount:
    return services.connect_account(ctx, "youtube", "http://127.0.0.1:8765/api/publish/youtube/callback", "code-1")


@pytest.fixture
def completed_render(ctx, talk_copy) -> str:
    """A real end-to-end render (sidecar transcription, small output) —
    publishing is tested against an actual file on disk, same as a user would.
    Uses its own worker and stops it before returning, so tests that start
    their own worker (e.g. restart recovery) don't fight it for the data
    dir's lock file."""
    from clipforge.jobs.worker import Worker
    w = Worker(ctx)
    assert w.start()
    try:
        p = services.create_project(ctx, "Pub")
        m, _ = services.import_media(ctx, p.id, str(talk_copy), auto_process=True)
        assert w.wait_idle(60)
        with ctx.session() as s:
            best = s.query(CandidateClip).filter_by(media_id=m.id).order_by(CandidateClip.score.desc()).first()
            cand_id = best.id
        services.review_candidate(ctx, cand_id, "approve")
        assert w.wait_idle(60)
        with ctx.session() as s:
            c = s.get(CandidateClip, cand_id)
            render_id = c.renders[-1].id
            assert c.renders[-1].status == "completed"
        return render_id
    finally:
        w.stop()


# --------------------------------------------------------------------------- account connection
def test_authorize_url_round_trips_state(ctx, fake_adapter):
    url, state = services.publish_authorize_url(ctx, "youtube", "http://x/callback")
    assert state in url and "http://x/callback" in url


def test_connect_account_creates_it_and_stores_the_token(ctx, fake_adapter):
    acc = services.connect_account(ctx, "youtube", "http://x/callback", "code-1")
    assert acc.platform == "youtube" and acc.display_name == "My Channel" and acc.status == "connected"
    assert fake_adapter.exchange_calls == ["code-1"]
    from clipforge.publishing import credentials
    assert credentials.load_token(acc.credential_key) == {"token": "tok", "refresh_token": "rtok"}


def test_reconnecting_the_same_account_updates_it_in_place(ctx, fake_adapter):
    acc1 = services.connect_account(ctx, "youtube", "http://x/callback", "code-1")
    fake_adapter.account_info = AccountInfo(external_account_id="chan-1", display_name="Renamed Channel")
    acc2 = services.connect_account(ctx, "youtube", "http://x/callback", "code-2")
    assert acc1.id == acc2.id and acc2.display_name == "Renamed Channel"
    with ctx.session() as s:
        assert s.query(PublishAccount).count() == 1


def test_disconnect_removes_the_token_and_marks_disconnected(ctx, connected_account):
    from clipforge.publishing import credentials
    services.disconnect_account(ctx, connected_account.id)
    with ctx.session() as s:
        assert s.get(PublishAccount, connected_account.id).status == "disconnected"
    assert credentials.load_token(connected_account.credential_key) is None


def test_auto_publish_requires_a_connected_account(ctx, connected_account):
    services.disconnect_account(ctx, connected_account.id)
    with pytest.raises(AccountNotConnected):
        services.set_account_auto_publish(ctx, connected_account.id, True)


# --------------------------------------------------------------------------- request_publish
def test_request_publish_refuses_an_unconnected_account(ctx, connected_account, completed_render):
    services.disconnect_account(ctx, connected_account.id)
    with pytest.raises(AccountNotConnected):
        services.request_publish(ctx, completed_render, connected_account.id)


def test_request_publish_refuses_an_incomplete_render(ctx, connected_account, completed_render):
    with ctx.session() as s:
        r = s.get(RenderedClip, completed_render)
        r.status = "rendering"
    with pytest.raises(Conflict):
        services.request_publish(ctx, completed_render, connected_account.id)


def test_request_publish_dedupes_an_active_job_for_the_same_render_and_account(ctx, connected_account, completed_render):
    pj1 = services.request_publish(ctx, completed_render, connected_account.id)
    pj2 = services.request_publish(ctx, completed_render, connected_account.id)
    assert pj1.id == pj2.id
    with ctx.session() as s:
        assert s.query(PublishJob).count() == 1


# --------------------------------------------------------------------------- end-to-end job
def test_publish_job_uploads_and_records_the_result(ctx, completed_render, worker, fake_adapter, connected_account):
    pj = services.request_publish(ctx, completed_render, connected_account.id, title="My clip",
                                  tags=["a", "b"], privacy="unlisted")
    assert worker.wait_idle(30)
    with ctx.session() as s:
        row = s.get(PublishJob, pj.id)
        assert row.status == "uploaded"
        assert row.platform_post_url == "https://youtu.be/vid1"
        assert row.platform_upload_id == "vid1"
    token_data, req = fake_adapter.publish_calls[0]
    assert req.title == "My clip" and req.tags == ["a", "b"] and req.privacy == "unlisted"
    assert req.file_path.is_file()
    # refresh() is always called before publish — tokens near expiry still work.
    assert fake_adapter.refresh_calls


def test_publish_job_defaults_metadata_from_the_clip(ctx, completed_render, worker, fake_adapter, connected_account):
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    assert worker.wait_idle(30)
    assert pj.title or pj.description  # came from the candidate, not left blank


def test_reauth_required_marks_the_account_and_the_job(ctx, completed_render, worker, fake_adapter, connected_account):
    fake_adapter.refresh_result = ReauthRequired("token revoked")
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        row = s.get(PublishJob, pj.id)
        assert row.status == "needs_action"
        assert row.error_code == "REAUTH_REQUIRED"
        assert s.get(PublishAccount, connected_account.id).status == "needs_reauth"


def test_platform_not_supported_is_reported_as_needs_action_not_a_bug(
        ctx, completed_render, worker, fake_adapter, connected_account):
    fake_adapter.result = PlatformNotSupported("unaudited app, draft only",
                                               hint="Finish it in the app.")
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        row = s.get(PublishJob, pj.id)
        assert row.status == "needs_action"
        assert "draft" in row.error.lower()
        # a known limitation isn't retried like a transient failure
        job = s.query(Job).filter_by(id=row.job_id).one()
        assert job.attempts == 1


def test_a_draft_result_is_never_reported_as_published(ctx, completed_render, worker, fake_adapter, connected_account):
    fake_adapter.result = PublishResult(status="draft", platform_upload_id="pub1",
                                        note="Sent to inbox as a private draft.")
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        row = s.get(PublishJob, pj.id)
        assert row.status == "draft"
        assert row.published_at is None


def test_transient_publish_failure_is_recorded_as_failed(ctx, completed_render, worker, fake_adapter, connected_account):
    from clipforge.errors import PublishFailed
    fake_adapter.result = PublishFailed("upload interrupted")
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        row = s.get(PublishJob, pj.id)
        assert row.status == "failed"
        assert row.retry_count >= 1


# --------------------------------------------------------------------------- cancel / retry
def test_cancel_a_queued_publish_job_never_runs(ctx, fake_adapter, connected_account, completed_render):
    # No worker running: the job stays queued until we cancel it.
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    cancelled = services.cancel_publish(ctx, pj.id)
    assert cancelled.status == "failed" and cancelled.error_code == "CANCELLED"
    assert not fake_adapter.publish_calls


def test_retry_a_failed_publish_job_runs_again(ctx, completed_render, worker, fake_adapter, connected_account):
    from clipforge.errors import PublishFailed
    fake_adapter.result = PublishFailed("flaky", retryable=False)
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        assert s.get(PublishJob, pj.id).status == "failed"

    fake_adapter.result = PublishResult(status="uploaded", platform_post_url="https://youtu.be/retried")
    services.retry_publish(ctx, pj.id)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        row = s.get(PublishJob, pj.id)
        assert row.status == "uploaded" and row.platform_post_url == "https://youtu.be/retried"


def test_retry_refuses_a_job_that_is_not_failed(ctx, fake_adapter, connected_account, completed_render):
    pj = services.request_publish(ctx, completed_render, connected_account.id)
    with pytest.raises(Conflict):
        services.retry_publish(ctx, pj.id)


# --------------------------------------------------------------------------- restart recovery
def test_publish_jobs_recover_after_a_restart(ctx, completed_render, fake_adapter, connected_account):
    """Same simulated-crash technique as test_jobs.py: claim the job as a
    'dead process' (no real worker thread involved, so this is deterministic),
    close the context, then start a fresh one over the same data dir — the
    standard recovery path, not something publish jobs special-case."""
    from clipforge.context import AppContext

    pj = services.request_publish(ctx, completed_render, connected_account.id)
    job_id = pj.job_id
    claimed = queue.claim(ctx.sf, lane="publish", worker_id="dead-process", lease_s=600)
    assert claimed.id == job_id
    with ctx.session() as s:
        assert s.get(Job, job_id).status == "running"
    ctx.close()

    ctx2 = AppContext(ctx.settings)
    try:
        with ctx2.session() as s:
            assert s.get(Job, job_id).status == "running"  # persisted, not lost
        from clipforge.jobs.worker import Worker
        w = Worker(ctx2)
        assert w.start()  # start() itself recovers orphaned jobs back to queued
        try:
            assert w.wait_idle(30)
        finally:
            w.stop()
        with ctx2.session() as s:
            row = s.get(PublishJob, pj.id)
            assert row.status == "uploaded"
            assert s.get(Job, job_id).attempts == 2  # the interrupted attempt counts
    finally:
        ctx2.close()


# --------------------------------------------------------------------------- unconfigured platform
def test_unconfigured_platform_gives_an_actionable_error(ctx):
    from clipforge.publishing.tiktok import TikTokAdapter
    from clipforge.errors import ValidationFailed
    with pytest.raises(ValidationFailed) as e:
        TikTokAdapter().authorize_url(ctx.settings, "http://x/callback", "state1")
    assert "CLIPFORGE_TIKTOK_CLIENT_KEY" in e.value.hint


def test_tiktok_refuses_non_private_publish_without_audit(ctx):
    from clipforge.publishing.tiktok import TikTokAdapter
    from clipforge.publishing.base import PublishRequest
    from pathlib import Path
    with pytest.raises(PlatformNotSupported) as e:
        TikTokAdapter().publish({"access_token": "x"},
                                PublishRequest(file_path=Path("x.mp4"), title="t", description="",
                                              tags=[], privacy="public"))
    assert "draft" in e.value.hint.lower() or "inbox" in e.value.hint.lower()
