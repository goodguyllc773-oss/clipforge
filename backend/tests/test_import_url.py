"""Test 18: import by URL — classification, successful download+import, and
failure cases (bad URL, network failure, unsupported site). The downloader is
mocked everywhere except one real loopback-HTTP test, so none of this needs
real network access."""

from __future__ import annotations

import builtins
import http.server
import threading
from pathlib import Path

import pytest

from clipforge import services
from clipforge.db.models import Job, SourceMedia
from clipforge.errors import DependencyMissing, DownloadFailed, InvalidUrl, JobCancelled, UnsupportedSource
from clipforge.jobs import queue
from clipforge.media import download


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(queue, "RETRY_BASE_S", 0.02)
    monkeypatch.setattr(queue, "RETRY_MAX_S", 0.05)


# --------------------------------------------------------------------------- classification
@pytest.mark.parametrize("url,expected", [
    ("https://cdn.example.com/clip.MP4", "direct"),
    ("https://cdn.example.com/clip.mp4?token=abc&x=1", "direct"),
    ("https://cdn.example.com/path/video.mov#frag", "direct"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "ytdlp"),
    ("https://www.tiktok.com/@someone/video/123456", "ytdlp"),
    ("https://cdn.example.com/no-extension-stream", "ytdlp"),
])
def test_classify_url(url, expected):
    assert download.classify_url(url) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not a url", "ftp://host/file.mp4", "mp4-but-no-scheme.com/x.mp4"])
def test_validate_url_rejects_bad_input(raw):
    with pytest.raises(InvalidUrl):
        download.validate_url(raw)


def test_validate_url_trims_whitespace():
    assert download.validate_url("  https://example.com/a.mp4  ") == "https://example.com/a.mp4"


def test_fetch_dispatches_direct_vs_ytdlp(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(download, "download_direct",
                        lambda url, dest, **kw: (calls.append("direct"), tmp_path / "a.mp4")[1])
    monkeypatch.setattr(download, "download_via_ytdlp",
                        lambda url, dest, **kw: (calls.append("ytdlp"), tmp_path / "b.mp4")[1])
    download.fetch("https://cdn.example.com/a.mp4", tmp_path)
    download.fetch("https://www.youtube.com/watch?v=x", tmp_path)
    assert calls == ["direct", "ytdlp"]


# --------------------------------------------------------------------------- download_via_ytdlp
def test_download_via_ytdlp_missing_dependency(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "yt_dlp":
            raise ImportError("no module named yt_dlp")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(DependencyMissing):
        download.download_via_ytdlp("https://www.youtube.com/watch?v=x", tmp_path)


# --------------------------------------------------------------------------- download_direct (real loopback HTTP)
class _Handler(http.server.BaseHTTPRequestHandler):
    body = b"not a real video, just bytes" * 10000

    def do_GET(self):  # noqa: N802
        if self.path == "/missing.mp4":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def http_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_download_direct_streams_to_disk_with_progress(tmp_path, http_server):
    seen = []
    out = download.download_direct(f"{http_server}/video.mp4", tmp_path, on_progress=seen.append)
    assert out.name == "video.mp4"
    assert out.read_bytes() == _Handler.body
    assert seen and seen[-1] == pytest.approx(1.0)
    assert not out.with_name(out.name + ".part").exists()


def test_download_direct_404_is_not_retryable(tmp_path, http_server):
    with pytest.raises(DownloadFailed) as e:
        download.download_direct(f"{http_server}/missing.mp4", tmp_path)
    assert e.value.retryable is False
    assert not list(tmp_path.glob("*.part"))


def test_download_direct_cancellation_cleans_up_partial_file(tmp_path, http_server):
    def cancel_now():
        raise JobCancelled()

    with pytest.raises(JobCancelled):
        download.download_direct(f"{http_server}/video.mp4", tmp_path, cancel_check=cancel_now)
    assert not list(tmp_path.glob("*"))


def test_download_direct_connection_refused_is_retryable(tmp_path):
    with pytest.raises(DownloadFailed) as e:
        download.download_direct("http://127.0.0.1:1/video.mp4", tmp_path)
    assert e.value.retryable is True


# --------------------------------------------------------------------------- services.import_media_from_url
def test_import_media_from_url_rejects_bad_url_before_enqueuing(ctx):
    p = services.create_project(ctx, "Links")
    with pytest.raises(InvalidUrl):
        services.import_media_from_url(ctx, p.id, "not a url")
    with ctx.session() as s:
        assert s.query(Job).count() == 0


def test_import_media_from_url_enqueues_a_download_job(ctx):
    p = services.create_project(ctx, "Links")
    job = services.import_media_from_url(ctx, p.id, "https://cdn.example.com/clip.mp4")
    assert job.kind == "import_url" and job.lane == "download" and job.status == "queued"
    assert job.payload["url"] == "https://cdn.example.com/clip.mp4"
    # Re-importing the same URL into the same project while it's active is deduped.
    job2 = services.import_media_from_url(ctx, p.id, "https://cdn.example.com/clip.mp4")
    assert job2.id == job.id


# --------------------------------------------------------------------------- end-to-end job (mocked downloader)
def test_import_url_direct_download_and_import_succeeds(ctx, worker, media, monkeypatch):
    def fake_fetch(url, dest_dir, *, on_progress=None, cancel_check=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "downloaded.mp4"
        dest.write_bytes(media["talk"]["video"].read_bytes())
        if on_progress:
            on_progress(1.0)
        return dest, "direct"

    monkeypatch.setattr(download, "fetch", fake_fetch)
    p = services.create_project(ctx, "Links")
    job = services.import_media_from_url(ctx, p.id, "https://cdn.example.com/talk.mp4", auto_process=False)
    assert worker.wait_idle(30)
    with ctx.session() as s:
        j = s.get(Job, job.id)
        assert j.status == "completed", j.error
        media_id = j.result["media_id"]
        assert j.result["kind"] == "direct"
        m = s.get(SourceMedia, media_id)
        assert m.source_url == "https://cdn.example.com/talk.mp4"
        assert m.status == "ready" and m.has_audio and m.duration_s > 60
        # The download landed under <data dir>/downloads/<job_id>/ and stayed there
        # as the media's permanent source, same as a locally-browsed file.
        assert Path(m.original_path).is_file()
        assert ctx.settings.downloads_dir in Path(m.original_path).parents


def test_import_url_network_failure_retries_then_fails(ctx, worker, monkeypatch):
    calls = {"n": 0}

    def flaky_fetch(url, dest_dir, **kw):
        calls["n"] += 1
        raise DownloadFailed("connection reset")

    monkeypatch.setattr(download, "fetch", flaky_fetch)
    p = services.create_project(ctx, "Links")
    job = services.import_media_from_url(ctx, p.id, "https://cdn.example.com/flaky.mp4")
    assert worker.wait_idle(30)
    with ctx.session() as s:
        j = s.get(Job, job.id)
        assert j.status == "failed"
        assert j.error_code == "DOWNLOAD_FAILED"
    assert calls["n"] == j.max_attempts  # retried up to the limit, not stuck looping
    # Nothing left behind in the downloads dir for the failed job.
    assert not (ctx.settings.downloads_dir / job.id).exists()


def test_import_url_unsupported_site_fails_without_retrying(ctx, worker, monkeypatch):
    def unsupported_fetch(url, dest_dir, **kw):
        raise UnsupportedSource("that link isn't from a supported site")

    monkeypatch.setattr(download, "fetch", unsupported_fetch)
    p = services.create_project(ctx, "Links")
    job = services.import_media_from_url(ctx, p.id, "https://example.com/not-a-video-site")
    assert worker.wait_idle(10)
    with ctx.session() as s:
        j = s.get(Job, job.id)
        assert j.status == "failed" and j.error_code == "UNSUPPORTED_SOURCE"
        assert j.attempts == 1  # non-retryable: fails on the first try
