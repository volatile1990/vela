import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vela.api import create_app
from vela.config import Settings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / ".vela-volume").write_text("test-volume")
    settings = Settings(state_dir=tmp_path / "state", work_dir=tmp_path / "work", download_dir=downloads, credentials_dir=tmp_path / "credentials", base_url="http://testserver", storage_marker="test-volume", frontend_dir=tmp_path / "frontend")
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app, settings


def test_trusted_network_access_needs_no_credentials_but_enforces_host_origin(setup):
    client, _, _ = setup
    assert client.get("/api/health").status_code == 200
    response = client.get("/api/session")
    assert response.json() == {"authenticated": True, "username": None, "csrf_token": None, "version": "1.0.0"}
    assert "set-cookie" not in response.headers
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 400
    body = {"url": "https://example.com/a"}
    assert client.post("/api/jobs", json=body).status_code == 403
    assert client.post("/api/jobs", json=body, headers={"Origin": "http://attacker.invalid"}).status_code == 403
    assert client.post("/api/jobs", json=body, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/jobs", json=body, headers={"Origin": "http://testserver"}).status_code == 202
    for endpoint in ("login", "logout"):
        assert client.get(f"/api/{endpoint}").status_code == 404
        assert client.post(f"/api/{endpoint}", json={}, headers={"Origin": "http://testserver"}).status_code == 404


def test_validation_never_echoes_signed_source_url(setup):
    client, _, _ = setup
    url = "file:///private?token=sensitive-signed-source"
    response = client.post("/api/jobs", json={"url": url}, headers={"Origin": "http://testserver"})
    assert response.status_code == 422
    assert "sensitive-signed-source" not in response.text


def test_worker_heartbeat_and_status_are_exposed_with_stale_detection(setup):
    client, app, _ = setup
    app.state.database.set_worker_status({"status": "running", "heartbeat_at": "2000-01-01T00:00:00Z", "storage": {"available": True, "free_bytes": 123}})
    result = client.get("/api/overview").json()
    assert result["worker"]["status"] == "offline"
    assert result["storage"]["available"]
    assert app.state.database.events_after(0)[-1]["data"] == {"worker": True}


def test_job_roundtrip_idempotency_validation_cancel_retry(setup):
    client, _, _ = setup
    headers = {"Origin": "http://testserver"}
    body = {"url": "https://example.com/video", "profile": "audio", "max_height": None}
    response = client.post("/api/jobs", json=body, headers={**headers, "Idempotency-Key": "one"})
    assert response.status_code == 202
    job = response.json()
    repeated = client.post("/api/jobs", json=body, headers={**headers, "Idempotency-Key": "one"})
    assert repeated.json()["id"] == job["id"]
    assert client.get("/api/jobs").json()["total"] == 1
    assert client.post("/api/jobs/"+job["id"]+"/cancel", headers=headers).json()["status"] == "cancelled"
    assert client.post("/api/jobs/"+job["id"]+"/retry", headers=headers).json()["status"] == "queued"
    assert client.post("/api/jobs/"+job["id"]+"/retry", headers=headers).status_code == 409
    assert client.post("/api/previews", json=body, headers=headers).json()["kind"] == "probe"
    for invalid in ["file:///etc/passwd", "http://127.0.0.1/x", "http://[::1]/x", "http://user:secret@example.com/x", "http://vela.home.arpa/x"]:
        assert client.post("/api/jobs", json={"url": invalid}, headers=headers).status_code == 422
    assert client.post("/api/jobs", json={"url": body["url"], "playlist_items": "1-1000"}, headers=headers).status_code == 422


def test_artifact_download_marker_traversal_range_and_missing_file(setup, tmp_path):
    client, app, settings = setup
    folder = settings.download_dir / "job"
    folder.mkdir()
    (folder / "video.mp4").write_bytes(b"0123456789")
    database = app.state.database
    job = database.create_job(url="https://example.com/a")
    artifact = database.add_artifact(job["id"], relative_path="job/video.mp4", name="Vidéö.mp4", size=10, mime="video/mp4", sha256="a"*64)
    url = f"/api/artifacts/{artifact['id']}/download"
    response = client.get(url)
    assert response.content == b"0123456789"
    assert response.headers["content-disposition"].startswith("attachment;")
    response = client.get(url, headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    assert client.get(url, headers={"Range": "bytes=100-200"}).status_code == 416
    bad = database.add_artifact(job["id"], relative_path="../password", name="x", size=19, mime="text/plain", sha256="x")
    assert client.get(f"/api/artifacts/{bad['id']}/download").status_code == 404
    (settings.download_dir / ".vela-volume").write_text("wrong")
    assert client.get(url).status_code == 503


@pytest.mark.skipif(os.name != "posix", reason="Production uses Linux openat flags")
def test_artifact_rejects_symlink_parent(setup, tmp_path):
    client, app, settings = setup
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (settings.download_dir / "link").symlink_to(outside, target_is_directory=True)
    job = app.state.database.create_job(url="https://example.com/a")
    artifact = app.state.database.add_artifact(job["id"], relative_path="link/secret", name="x", size=6, mime="text/plain", sha256="x")
    assert client.get(f"/api/artifacts/{artifact['id']}/download").status_code == 404


def test_sse_snapshot_and_replay_share_committed_job_events(setup):
    client, app, _ = setup
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/events")

    async def run():
        async def connected():
            return False
        request = SimpleNamespace(headers={}, app=app, is_disconnected=connected)
        response = await endpoint(request)
        iterator = response.body_iterator
        snapshot = await anext(iterator)
        assert '"snapshot":true' in snapshot
        job = app.state.database.create_job(url="https://example.com/a")
        event = await anext(iterator)
        assert job["id"] in event and "event: changed" in event
        await iterator.aclose()

    asyncio.run(run())


def test_frontend_fallback_never_masks_api_404(setup):
    client, _, settings = setup
    settings.frontend_dir.mkdir()
    (settings.frontend_dir / "index.html").write_text("<h1>Vela</h1>")
    assert client.get("/history").status_code == 200
    assert client.get("/api/not-found").status_code == 404
