import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from vela.config import Settings
from vela.db import Database
from vela.engine import DownloadResult, EngineError, EngineCancelled
from vela.worker import Worker, WorkerLock, selected_indexes


class FakeEngine:
    downloads = 0

    async def download(self, url, work_dir, *, on_event=None, **kwargs):
        self.downloads += 1
        file = work_dir / "media.mp4"
        file.write_bytes(b"a complete test video")
        metadata = {"title": "Video", "id": "id1", "extractor": "test"}
        if on_event:
            await on_event("metadata", metadata)
        return DownloadResult([file], metadata)

    async def probe(self, url, work_dir, **kwargs):
        return {"title": "Collection", "id": "list", "extractor": "test", "type": "playlist",
            "entries": [{"title": "A", "id": "a", "url": "https://example.com/a"},
                        {"title": "B", "id": "b", "url": "https://example.com/b"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")
    state, work, nas = [tmp_path / name for name in ("state", "work", "nas")]
    work.mkdir()
    nas.mkdir()
    (nas / ".vela-volume").write_text("test-nas")
    settings = Settings(state_dir=state, work_dir=work, download_dir=nas,
        storage_marker="test-nas", min_free_bytes=0)
    db = Database(settings.db_path)
    db.initialize()
    return settings, db, FakeEngine()


def test_worker_publishes_artifact_and_cleans_work(setup):
    settings, db, engine = setup
    job = db.create_job(url="https://example.com/video")
    worker = Worker(settings, db=db, engine=engine)
    asyncio.run(worker.handle(db.claim_next()))
    result = db.get_job(job["id"])
    assert result["status"] == "completed"
    assert len(result["artifacts"]) == 1
    assert (settings.download_dir / result["artifacts"][0]["relative_path"]).exists()
    assert not (settings.work_dir / job["id"]).exists()


def test_nas_outage_and_restart_finish_without_redownload(setup):
    settings, db, engine = setup
    job = db.create_job(url="https://example.com/video")
    marker = settings.download_dir / ".vela-volume"
    marker.unlink()
    worker = Worker(settings, db=db, engine=engine)
    asyncio.run(worker.handle(db.claim_next()))
    assert db.get_job(job["id"])["status"] == "waiting_storage"
    assert engine.downloads == 1
    restarted = Worker(settings, db=db, engine=engine)
    with WorkerLock(settings.state_dir):
        restarted.recover()
    asyncio.run(restarted.retry_finalizations())
    assert db.get_job(job["id"])["status"] == "waiting_storage"
    marker.write_text("test-nas")
    asyncio.run(restarted.retry_finalizations())
    assert db.get_job(job["id"])["status"] == "completed"
    assert engine.downloads == 1
    assert len(db.get_job(job["id"])["artifacts"]) == 1


def test_worker_lock_excludes_second_scheduler(setup):
    settings, _, _ = setup
    with WorkerLock(settings.state_dir):
        with pytest.raises(RuntimeError):
            with WorkerLock(settings.state_dir):
                pass


def test_playlist_expansion_has_idempotent_children(setup):
    settings, db, engine = setup
    parent = db.create_job(url="https://example.com/list", playlist=True)
    worker = Worker(settings, db=db, engine=engine)
    asyncio.run(worker.handle(db.claim_next()))
    assert db.get_job(parent["id"])["status"] == "completed"
    assert db.count_jobs() == 3
    # Repeating expansion after crash/retry never duplicates the child jobs.
    info = asyncio.run(engine.probe("https://example.com/list", settings.work_dir))
    asyncio.run(worker._expand_playlist(parent, info, asyncio.Event()))
    assert db.count_jobs() == 3


def test_playlist_selection_is_bounded():
    assert selected_indexes("1,3-4", 5, 100) == [0, 2, 3]
    assert selected_indexes(None, 1000, 100) == list(range(100))
    with pytest.raises(EngineError):
        selected_indexes("1-1000000000", 100, 100)


def test_crash_recovery_requeues_download_but_keeps_parts(setup):
    settings, db, engine = setup
    job = db.create_job(url="https://example.com/video")
    db.claim_next()
    directory = settings.work_dir / job["id"]
    directory.mkdir()
    part = directory / "media.mp4.part"
    part.write_bytes(b"partial")
    worker = Worker(settings, db=db, engine=engine)
    with WorkerLock(settings.state_dir):
        worker.recover()
    assert db.get_job(job["id"])["status"] == "queued"
    assert part.read_bytes() == b"partial"


def test_preview_slot_runs_during_active_download(setup):
    settings, db, _ = setup
    settings = replace(settings, poll_interval=0.1)

    class SlowEngine(FakeEngine):
        async def download(self, url, work_dir, *, cancel, **kwargs):
            await cancel.wait()
            raise EngineCancelled()

    download = db.create_job(url="https://example.com/long")
    preview = db.create_job(url="https://example.com/preview", kind="probe")
    worker = Worker(settings, db=db, engine=SlowEngine())

    async def run():
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if db.get_job(preview["id"])["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert db.get_job(preview["id"])["status"] == "completed"
            assert db.get_job(download["id"])["status"] == "downloading"
        finally:
            worker.request_stop()
            await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert db.get_job(download["id"])["status"] == "interrupted"


def test_retry_clears_previous_finalization_before_new_engine_attempt(setup):
    settings, db, _ = setup
    job = db.create_job(url="https://example.com/video")
    db.update_job(job["id"], status="completed", metadata={"finalization": [{"source": "old-missing.mp4", "title": "Old"}]})
    db.retry_job(job["id"])
    claimed = db.claim_next()
    worker = Worker(settings, db=db, engine=FakeEngine())
    # A crash between transactional claim and task start must also requeue this
    # fresh attempt rather than try publishing the old missing source.
    worker.recover()
    assert db.get_job(job["id"])["status"] == "queued"

    class CheckNewAttempt(FakeEngine):
        async def download(self, *args, **kwargs):
            assert "finalization" not in db.get_job(job["id"])["metadata"]
            raise EngineError("Neue Quelle momentan nicht erreichbar.", "network")

    worker.engine = CheckNewAttempt()
    asyncio.run(worker.handle(db.claim_next()))
    result = db.get_job(job["id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "network"
    assert "finalization" not in result["metadata"]


def test_target_conflict_is_terminal_instead_of_infinite_storage_retry(setup):
    settings, db, engine = setup
    job = db.create_job(url="https://example.com/video")
    target = settings.download_dir / job["id"]
    target.mkdir()
    (target / "Video.mp4").write_bytes(b"a different existing media file")
    worker = Worker(settings, db=db, engine=engine)
    asyncio.run(worker.handle(db.claim_next()))
    result = db.get_job(job["id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "conflict"
    asyncio.run(worker.retry_finalizations())
    assert db.get_job(job["id"])["status"] == "failed"
    assert (target / "Video.mp4").read_bytes() == b"a different existing media file"


def test_full_queue_keeps_existing_playlist_children_without_duplicates(setup):
    settings, db, engine = setup
    settings = replace(settings, max_queue_size=1)
    job = db.create_job(url="https://example.com/list", playlist=True)
    worker = Worker(settings, db=db, engine=engine)
    asyncio.run(worker.handle(db.claim_next()))
    result = db.get_job(job["id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "queue_full"
    assert db.count_jobs() == 2
    info = asyncio.run(engine.probe(job["url"], settings.work_dir))
    with pytest.raises(EngineError) as error:
        asyncio.run(worker._expand_playlist(job, info, asyncio.Event()))
    assert error.value.category == "queue_full"
    assert db.count_jobs() == 2


@pytest.mark.parametrize("status", ["queued", "downloading", "finalizing", "waiting_storage"])
def test_crash_recovery_removes_cookie_copy_after_process_reconciliation(setup, monkeypatch, status):
    settings, db, engine = setup
    job = db.create_job(url="https://example.com/video")
    metadata = {"finalization": [{"source": "media.mp4", "title": "Video"}]} if status in ("finalizing", "waiting_storage") else {}
    db.update_job(job["id"], status=status, metadata=metadata)
    directory = settings.work_dir / job["id"]
    directory.mkdir()
    cookie = directory / ".vela-cookies.txt"
    cookie.write_text("synthetic-crash-session")
    part = directory / "media.mp4.part"
    part.write_bytes(b"partial media stays")
    observed = []

    def reconcile(path):
        assert path == directory
        assert cookie.exists()  # Never remove session material from a live child.
        observed.append(path)

    monkeypatch.setattr("vela.worker.recover_process", reconcile)
    with WorkerLock(settings.state_dir):
        Worker(settings, db=db, engine=engine).recover()
    assert observed == [directory]
    assert not cookie.exists()
    assert part.read_bytes() == b"partial media stays"
    expected = "waiting_storage" if status in ("finalizing", "waiting_storage") else "queued"
    assert db.get_job(job["id"])["status"] == expected


def test_crash_cookie_is_retained_if_old_process_cannot_be_safely_reconciled(setup, monkeypatch):
    settings, db, engine = setup
    job = db.create_job(url="https://example.com/video")
    db.update_job(job["id"], status="downloading")
    directory = settings.work_dir / job["id"]
    directory.mkdir()
    cookie = directory / ".vela-cookies.txt"
    cookie.write_text("synthetic-crash-session")

    def unsafe(_):
        raise EngineError("Vorheriger Prozess ist noch aktiv.", "recovery")

    monkeypatch.setattr("vela.worker.recover_process", unsafe)
    with WorkerLock(settings.state_dir), pytest.raises(EngineError):
        Worker(settings, db=db, engine=engine).recover()
    assert cookie.read_text() == "synthetic-crash-session"
