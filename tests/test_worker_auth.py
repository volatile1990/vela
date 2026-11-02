"""Durable worker coordination with the real auth-state database, synthetic data."""
import asyncio
from dataclasses import replace

import pytest

from vela.config import Settings
from vela.db import Database
from vela.engine import DownloadResult, EngineError
from vela.instagram_auth import InstagramAuthState
from vela.source_sessions import SourceSessionStore
from vela.worker import Worker, WorkerLock, AUTH_ATTENTION_MESSAGE


class SyntheticEngine:
    def __init__(self):
        self.calls = 0
        self.errors = []

    async def download(self, url, work_dir, *, on_event=None, **kwargs):
        self.calls += 1
        metadata = {"title": "Synthetic", "id": "synthetic", "extractor": "synthetic"}
        if on_event:
            await on_event("metadata", metadata)
        if self.errors:
            raise self.errors.pop(0)
        file = work_dir / "media.mp4"
        file.write_bytes(b"synthetic media")
        return DownloadResult([file], metadata)

    async def probe(self, url, work_dir, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"title": "Synthetic", "id": "synthetic", "extractor": "synthetic", "type": "video"}


@pytest.fixture
def auth_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")
    # Keep all test job URLs synthetic; production host matching is tested in
    # the source-session suite and is not replaced in the running application.
    monkeypatch.setattr("vela.worker.site_for_url", lambda url: "instagram" if "/auth-synthetic" in url else None)
    settings = Settings(state_dir=tmp_path / "state", work_dir=tmp_path / "work",
        download_dir=tmp_path / "nas", credentials_dir=tmp_path / "credentials",
        storage_marker="test-nas", min_free_bytes=0, poll_interval=0.1)
    settings.work_dir.mkdir()
    settings.download_dir.mkdir()
    (settings.download_dir / ".vela-volume").write_text("test-nas")
    db = Database(settings.db_path)
    db.initialize()
    auth = InstagramAuthState(settings.state_dir)
    auth.configure(True)
    sessions = SourceSessionStore(settings.credentials_dir)
    engine = SyntheticEngine()
    worker = Worker(settings, db=db, engine=engine, instagram_auth=auth, session_store=sessions)
    return settings, db, auth, sessions, engine, worker


def export_synthetic_session(sessions):
    sessions.import_text("instagram", "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsynthetic-session\n")


def new_job(db, kind="download"):
    return db.create_job(url="https://example.com/auth-synthetic", kind=kind)


def handle_next(worker, db, kind="download"):
    job = db.claim_next(kind=kind)
    assert job is not None
    asyncio.run(worker.handle(job))
    return db.get_job(job["id"])


def finish_ready(auth, sessions):
    export_synthetic_session(sessions)
    request_id = auth.status()["request_id"]
    assert request_id > 0
    auth.finish(request_id, "ready")


@pytest.mark.parametrize("kind", ["download", "probe"])
def test_missing_session_waits_without_browser_window_and_resumes_real_state(auth_setup, kind):
    _, db, auth, sessions, engine, worker = auth_setup
    job = new_job(db, kind)
    assert not auth.status()["browser_available"]
    waiting = handle_next(worker, db, kind)
    assert waiting["status"] == "waiting_auth"
    assert engine.calls == 0
    assert auth.status()["request_id"] == 1
    assert waiting["metadata"]["_instagram_auth"]["request_id"] == 1
    finish_ready(auth, sessions)
    worker.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "queued"
    completed = handle_next(worker, db, kind)
    assert completed["status"] == "completed"
    assert auth.status()["request_id"] == 1
    assert completed["metadata"]["_instagram_auth"]["attempted"] is True


def test_challenge_keeps_waiter_and_original_request_until_user_confirms(auth_setup):
    _, db, auth, sessions, engine, worker = auth_setup
    job = new_job(db)
    handle_next(worker, db)
    request_id = auth.status()["request_id"]
    auth.finish(request_id, "needs_attention")
    worker.retry_auth_waiters()
    waiting = db.get_job(job["id"])
    assert waiting["status"] == "waiting_auth"
    assert waiting["error"] == AUTH_ATTENTION_MESSAGE
    assert waiting["error_code"] == "auth_attention"
    assert auth.pending_request() == request_id
    assert engine.calls == 0
    finish_ready(auth, sessions)
    worker.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "queued"


def test_at_most_one_refresh_survives_engine_metadata_and_preserves_original_error(auth_setup):
    _, db, auth, sessions, engine, worker = auth_setup
    export_synthetic_session(sessions)
    engine.errors = [EngineError("Ursprüngliche Zugriffsbeschränkung.", "audience_restricted"),
                     EngineError("Spätere Anmeldeantwort.", "login_required")]
    job = new_job(db)
    assert handle_next(worker, db)["status"] == "waiting_auth"
    finish_ready(auth, sessions)
    worker.retry_auth_waiters()
    failed = handle_next(worker, db)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "audience_restricted"
    assert failed["error"] == "Ursprüngliche Zugriffsbeschränkung."
    assert auth.status()["request_id"] == 1
    assert failed["metadata"]["_instagram_auth"]["request_id"] == 1
    db.retry_job(job["id"])
    assert "_instagram_auth" not in db.get_job(job["id"])["metadata"]


def test_cooldown_zero_does_not_accept_old_ready_generation_and_restart_keeps_wait(auth_setup):
    settings, db, auth, sessions, engine, worker = auth_setup
    initial = auth.request_refresh()
    finish_ready(auth, sessions)
    engine.errors = [EngineError("Erneute Anmeldung erforderlich.", "login_required")]
    job = new_job(db)
    waiting = handle_next(worker, db)
    assert waiting["status"] == "waiting_auth"
    assert waiting["metadata"]["_instagram_auth"]["request_id"] is None
    restarted = Worker(settings, db=db, engine=engine, instagram_auth=auth, session_store=sessions)
    with WorkerLock(settings.state_dir):
        restarted.recover()
    restarted.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "waiting_auth"
    assert auth.status()["request_id"] == initial
    with auth._database.connection() as connection:
        connection.execute("UPDATE instagram_auth SET last_requested=0 WHERE id=1")
    restarted.retry_auth_waiters()
    assert auth.status()["request_id"] == initial + 1
    finish_ready(auth, sessions)
    restarted.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "queued"


def test_crash_between_request_and_job_checkpoint_uses_new_generation_without_second_request(auth_setup):
    settings, db, auth, sessions, engine, worker = auth_setup
    job = new_job(db)
    waiting = handle_next(worker, db)
    metadata = waiting["metadata"]
    metadata["_instagram_auth"]["request_id"] = None
    db.update_job(job["id"], metadata=metadata)
    finish_ready(auth, sessions)
    restarted = Worker(settings, db=db, engine=engine, instagram_auth=auth, session_store=sessions)
    with WorkerLock(settings.state_dir):
        restarted.recover()
    restarted.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "queued"
    assert auth.status()["request_id"] == 1


def test_auth_error_fails_and_does_not_retry_forever(auth_setup):
    _, db, auth, _, engine, worker = auth_setup
    job = new_job(db)
    handle_next(worker, db)
    auth.finish(auth.status()["request_id"], "error")
    worker.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "failed"
    assert db.get_job(job["id"])["error_code"] == "auth_failed"
    worker.retry_auth_waiters()
    assert auth.status()["request_id"] == 1
    assert engine.calls == 0


def test_cancelled_waiter_is_never_resurrected_by_ready_or_restart(auth_setup):
    settings, db, auth, sessions, engine, worker = auth_setup
    job = new_job(db)
    handle_next(worker, db)
    assert db.request_cancel(job["id"])["status"] == "cancelled"
    finish_ready(auth, sessions)
    worker.retry_auth_waiters()
    restarted = Worker(settings, db=db, engine=engine, instagram_auth=auth, session_store=sessions)
    with WorkerLock(settings.state_dir):
        restarted.recover()
    restarted.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "cancelled"
    assert db.claim_next() is None


def test_stop_preserves_waiter_and_next_worker_can_resume(auth_setup):
    settings, db, auth, sessions, engine, worker = auth_setup
    job = new_job(db)
    handle_next(worker, db)
    worker.request_stop()
    finish_ready(auth, sessions)
    worker.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "waiting_auth"
    restarted = Worker(settings, db=db, engine=engine, instagram_auth=auth, session_store=sessions)
    with WorkerLock(settings.state_dir):
        restarted.recover()
    restarted.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "queued"


def test_waiting_auth_does_not_block_other_downloads(auth_setup):
    _, db, auth, _, engine, worker = auth_setup
    waiting = new_job(db)
    other = db.create_job(url="https://example.com/other-synthetic")

    async def run():
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if db.get_job(other["id"])["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert db.get_job(waiting["id"])["status"] == "waiting_auth"
            assert db.get_job(other["id"])["status"] == "completed"
        finally:
            worker.request_stop()
            await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert engine.calls == 1
    assert auth.status()["request_id"] == 1


def test_unconfigured_account_keeps_existing_anonymous_behavior(auth_setup):
    _, db, auth, _, engine, worker = auth_setup
    auth.configure(False)
    engine.errors = [EngineError("Anmeldung benötigt.", "login_required")]
    new_job(db)
    failed = handle_next(worker, db)
    assert failed["status"] == "failed"
    assert failed["error"] == "Anmeldung benötigt."
    assert auth.status()["request_id"] == 0


def test_network_errors_do_not_launch_browser_login(auth_setup):
    _, db, auth, sessions, engine, worker = auth_setup
    export_synthetic_session(sessions)
    engine.errors = [EngineError("Verbindung unterbrochen.", "network")]
    new_job(db)
    assert handle_next(worker, db)["error_code"] == "network"
    assert auth.status()["request_id"] == 0


def test_expired_session_requests_refresh_before_downloader_starts(auth_setup):
    settings, db, auth, _, engine, worker = auth_setup
    settings.credentials_dir.mkdir()
    cookie = settings.credentials_dir / "instagram.cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1\tsessionid\tsynthetic-expired\n")
    cookie.chmod(0o600)
    new_job(db)
    assert handle_next(worker, db)["status"] == "waiting_auth"
    assert engine.calls == 0
    assert auth.status()["request_id"] == 1


def test_crash_before_request_receipt_does_not_spend_second_login_after_browser_error(auth_setup):
    settings, db, auth, _, engine, worker = auth_setup
    job = new_job(db)
    waiting = handle_next(worker, db)
    metadata = waiting["metadata"]
    metadata["_instagram_auth"]["request_id"] = None
    db.update_job(job["id"], metadata=metadata)
    auth.finish(1, "error")
    with auth._database.connection() as connection:
        connection.execute("UPDATE instagram_auth SET last_requested=0 WHERE id=1")
    restarted = Worker(settings, db=db, engine=engine, instagram_auth=auth)
    with WorkerLock(settings.state_dir):
        restarted.recover()
    restarted.retry_auth_waiters()
    assert db.get_job(job["id"])["status"] == "failed"
    assert auth.status()["request_id"] == 1
    assert engine.calls == 0


def test_multiple_waiters_share_one_login_request(auth_setup):
    _, db, auth, sessions, _, worker = auth_setup
    first = new_job(db)
    handle_next(worker, db)
    second = new_job(db)
    handle_next(worker, db)
    assert auth.status()["request_id"] == 1
    finish_ready(auth, sessions)
    worker.retry_auth_waiters()
    assert db.get_job(first["id"])["status"] == "queued"
    assert db.get_job(second["id"])["status"] == "queued"
