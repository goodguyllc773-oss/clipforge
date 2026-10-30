from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from media_fixtures import (  # noqa: E402
    make_flash_beep_video, make_json_transcript, make_scene_video, make_silent_video, make_talk_video,
)

from clipforge.config import load_settings  # noqa: E402
from clipforge.context import AppContext  # noqa: E402

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.exit("ffmpeg/ffprobe must be on PATH to run the ClipForge tests", returncode=3)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict:
    """Generated once per test session (a few seconds)."""
    d = tmp_path_factory.mktemp("media")
    talk = make_talk_video(d / "talk.mp4")
    make_json_transcript(d / "talk_words.json", talk["segments"])
    return {
        "dir": d,
        "talk": talk,
        "silent": make_silent_video(d / "silent.mp4"),
        "flash": make_flash_beep_video(d / "flash.mp4"),
        "scenes": make_scene_video(d / "scenes.mp4"),
    }


@pytest.fixture
def ctx(tmp_path) -> AppContext:
    settings = load_settings(
        data_dir=tmp_path / "data", transcriber="sidecar", lease_s=3.0,
        heartbeat_s=0.3, poll_s=0.05, embedded_worker=False, frontend_dist=None,
        # Never inherit a developer's real .env (publishing credentials, an
        # ngrok tunnel base for local TikTok testing): tests must see a
        # consistent "nothing configured" starting point regardless of what's
        # set up on the machine running them.
        youtube_client_id="", youtube_client_secret="", tiktok_client_key="", tiktok_client_secret="",
        oauth_redirect_base="http://127.0.0.1:8765", tiktok_redirect_base="", extra_allowed_origins=())
    c = AppContext(settings)
    # Small output keeps render tests fast; 1080x1920 is covered separately.
    c.update_prefs({"output_width": 540, "output_height": 960,
                    "default_export_dir": str(tmp_path / "exports")})
    yield c
    c.close()


@pytest.fixture
def worker(ctx):
    from clipforge.jobs.worker import Worker
    w = Worker(ctx)
    assert w.start()
    yield w
    w.stop()


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch):
    """No test may touch the real OS credential store — publishing tokens
    live in an in-memory dict backend instead, for every test in the suite."""
    from clipforge.publishing import credentials as cred_mod
    store: dict[tuple[str, str], str] = {}

    class _FakeKeyring:
        @staticmethod
        def set_password(service, key, value):
            store[(service, key)] = value

        @staticmethod
        def get_password(service, key):
            return store.get((service, key))

        @staticmethod
        def delete_password(service, key):
            store.pop((service, key), None)

    monkeypatch.setattr(cred_mod, "_backend", lambda: _FakeKeyring())


@pytest.fixture
def talk_copy(tmp_path, media) -> Path:
    """A private copy (with its .srt sidecar) that tests may move or delete."""
    d = tmp_path / "src"
    d.mkdir()
    shutil.copy(media["talk"]["video"], d / "talk.mp4")
    shutil.copy(media["talk"]["srt"], d / "talk.srt")
    return d / "talk.mp4"
