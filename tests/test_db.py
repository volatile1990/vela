from concurrent.futures import ThreadPoolExecutor

import pytest

from vela.db import Database, safe_sqlite_version


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")
    result = Database(tmp_path / "state" / "vela.sqlite3")
    result.initialize()
    return result


def test_claim_is_atomic_across_connections(database):
    job = database.create_job(url="https://example.com/video")
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: database.claim_next(), range(8)))
    claims = [result for result in results if result is not None]
    assert len(claims) == 1
    assert claims[0]["id"] == job["id"]
    assert database.get_job(job["id"])["attempt"] == 1


def test_idempotency_reuses_job_and_rejects_changed_request(database):
    first = database.create_job(url="https://example.com/a", idempotency_key="same")
    second = database.create_job(url="https://example.com/a", idempotency_key="same")
    assert first["id"] == second["id"]
    assert database.count_jobs() == 1
    with pytest.raises(ValueError, match="different request"):
        database.create_job(url="https://example.com/b", idempotency_key="same")


def test_queue_limit_is_atomic_and_existing_idempotency_key_still_works(database):
    first = database.create_job(url="https://example.com/a", idempotency_key="same", max_queued=1)
    assert database.create_job(url="https://example.com/a", idempotency_key="same", max_queued=1)["id"] == first["id"]
    with pytest.raises(OverflowError):
        database.create_job(url="https://example.com/b", max_queued=1)


def test_cancel_queue_then_retry_preserves_attempt_accounting(database):
    job = database.create_job(url="https://example.com/a")
    cancelled = database.request_cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    assert database.claim_next() is None
    retried = database.retry_job(job["id"])
    assert retried["status"] == "queued"
    assert not retried["cancel_requested"]
    active = database.claim_next()
    assert active["attempt"] == 1
    assert database.request_cancel(job["id"])["cancel_requested"]
    assert database.get_job(job["id"])["status"] == "probing"
    with pytest.raises(ValueError):
        database.retry_job(job["id"])


def test_claim_can_reserve_separate_probe_and_download_slots(database):
    download = database.create_job(url="https://example.com/download")
    preview = database.create_job(url="https://example.com/probe", kind="probe")
    assert database.claim_next(kind="probe")["id"] == preview["id"]
    assert database.claim_next(kind="probe") is None
    assert database.claim_next(kind="download")["id"] == download["id"]


def test_events_metadata_artifacts_survive_reopen(database):
    job = database.create_job(url="https://example.com/a")
    database.update_job(job["id"], metadata={"entries": [{"id": "abc"}]}, status="finalizing", progress=123)
    artifact = database.add_artifact(job["id"], relative_path="a/video.mp4", name="Video.mp4", size=12, mime="video/mp4", sha256="a"*64)
    duplicate = database.add_artifact(job["id"], relative_path="a/video.mp4", name="Video.mp4", size=12, mime="video/mp4", sha256="a"*64)
    assert artifact["id"] == duplicate["id"]
    reopened = Database(database.path)
    value = reopened.get_job(job["id"])
    assert value["metadata"]["entries"][0]["id"] == "abc"
    assert value["progress"] == 100
    assert len(value["artifacts"]) == 1
    assert reopened.events_after(0)[0]["data"] == {"job_id": job["id"]}
    assert reopened.active_jobs()[0]["id"] == job["id"]


def test_existing_database_with_legacy_session_table_keeps_jobs(database):
    job = database.create_job(url="https://example.com/retained")
    with database.connection() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires_at INTEGER NOT NULL)")
        connection.execute("INSERT INTO sessions VALUES('legacy-unused-session',9999999999)")
    reopened = Database(database.path)
    reopened.initialize()
    assert reopened.get_job(job["id"])["url"] == "https://example.com/retained"
    assert reopened.claim_next()["id"] == job["id"]


def test_update_cannot_modify_sql_identifiers(database):
    job = database.create_job(url="https://example.com/a")
    with pytest.raises(ValueError):
        database.update_job(job["id"], **{"url='oops'; --": "x"})


@pytest.mark.parametrize("version,expected", [("3.50.4", False),("3.51.2",False),("3.51.3",True),("3.52.0",True),("3.50.7",True),("3.44.6",True),("3.45.0",False)])
def test_sqlite_patch_version_detection(version, expected):
    assert safe_sqlite_version(version) is expected
