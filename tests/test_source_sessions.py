import http.cookiejar
import json
import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vela.api import create_app
from vela.config import Settings
from vela.source_sessions import MAX_COOKIE_BYTES, SourceSessionError, SourceSessionStore, site_for_url, validate_cookie_text


HEADER = "# Netscape HTTP Cookie File\n"


def row(domain=".instagram.com", name="sessionid", value="synthetic-session-only", expiry=None, http_only=True):
    expiry = int(time.time()) + 7200 if expiry is None else expiry
    return ("#HttpOnly_" if http_only else "") + f"{domain}\t{'TRUE' if domain.startswith('.') else 'FALSE'}\t/\tTRUE\t{expiry}\t{name}\t{value}\n"


def test_import_filters_other_domains_comments_and_never_exposes_values(tmp_path):
    store = SourceSessionStore(tmp_path / "credentials")
    text = HEADER + "# synthetic-account-identity comment\n" + row() + row(".unrelated.invalid", "other", "synthetic-unrelated-only")
    status = store.import_text("instagram", text)
    assert status["site"] == "instagram" and status["configured"]
    assert status["cookie_count"] == 1 and status["state"] == "configured"
    assert status["expires_at"].endswith("Z") and status["updated_at"].endswith("Z")
    assert "synthetic" not in json.dumps(status)
    written = (tmp_path / "credentials" / "instagram.cookies.txt").read_text()
    assert "synthetic-session-only" in written
    assert "synthetic-unrelated-only" not in written and "synthetic-account-identity" not in written
    assert len(list((tmp_path / "credentials").iterdir())) == 1
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "credentials" / "instagram.cookies.txt").stat().st_mode) == 0o600


def test_session_and_httponly_cookies_remain_interoperable(tmp_path):
    validated = validate_cookie_text("instagram", HEADER + row(expiry=0))
    assert validated.expires_at is None
    path = tmp_path / "synthetic.cookies.txt"
    path.write_text(validated.text)
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True)
    cookies = list(jar)
    assert len(cookies) == 1
    assert cookies[0].discard and cookies[0].expires is None
    assert cookies[0].has_nonstandard_attr("HTTPOnly")


@pytest.mark.parametrize("site,domain", [("youtube", ".youtube.com"), ("youtube", ".google.com"), ("tiktok", ".tiktok.com"), ("facebook", ".facebook.com"), ("x", ".x.com"), ("x", ".twitter.com")])
def test_each_fixed_site_accepts_only_its_domains(site, domain):
    assert validate_cookie_text(site, HEADER + row(domain)).cookie_count == 1


def test_exact_domain_boundaries_and_instagram_required_session():
    for domain in (".instagram.com.unrelated.invalid", ".evilinstagram.com", ".instagr.am"):
        with pytest.raises(SourceSessionError, match="keine passenden"):
            validate_cookie_text("instagram", HEADER + row(domain))
    with pytest.raises(SourceSessionError, match="sessionid"):
        validate_cookie_text("instagram", HEADER + row(name="csrftoken"))
    with pytest.raises(SourceSessionError, match="sessionid"):
        validate_cookie_text("instagram", HEADER + row(value=""))


@pytest.mark.parametrize("bad", [
    "not a cookie file",
    HEADER + "broken\tentry\n",
    HEADER + row(expiry="not-a-time"),
    HEADER + row(expiry=-1),
    HEADER + row(expiry=999999999999),
    HEADER + row(value="x"*8193),
    HEADER + row(value="bad\x00value"),
    HEADER + row(name="invalid name"),
    HEADER + row().replace("TRUE\t/", "FALSE\t/", 1),
    HEADER + row().replace("\t/\t", "\tnot-a-path\t"),
])
def test_malformed_rows_are_rejected_without_echoing_input(bad):
    with pytest.raises(SourceSessionError) as error:
        validate_cookie_text("instagram", bad)
    assert "synthetic-session-only" not in str(error.value)
    assert "bad\x00value" not in str(error.value)


def test_expired_entries_are_filtered_and_invalid_import_keeps_previous(tmp_path):
    store = SourceSessionStore(tmp_path / "credentials")
    store.import_text("instagram", HEADER + row() + row(name="expired", expiry=1))
    path = tmp_path / "credentials" / "instagram.cookies.txt"
    previous = path.read_bytes()
    assert "expired" not in previous.decode()
    with pytest.raises(SourceSessionError, match="abgelaufen"):
        store.import_text("instagram", HEADER + row(expiry=1))
    assert path.read_bytes() == previous
    path.write_text(HEADER + row(expiry=1))
    assert store.status("instagram")["state"] == "expired"
    store.delete("instagram")
    assert store.status("instagram")["state"] == "missing"
    store.delete("instagram")


def test_missing_store_and_unknown_sites_have_safe_status(tmp_path):
    store = SourceSessionStore(tmp_path / "absent")
    assert len(store.items()) == 5
    assert all(item["state"] == "missing" and not item["configured"] for item in store.items())
    store.delete("instagram")
    with pytest.raises(SourceSessionError) as error:
        store.import_text("../escape", HEADER + row())
    assert error.value.status_code == 404
    assert not (tmp_path / "escape.cookies.txt").exists()


def test_failed_atomic_replace_keeps_old_jar_and_removes_temporary(tmp_path, monkeypatch):
    store = SourceSessionStore(tmp_path / "credentials")
    store.import_text("instagram", HEADER + row())
    target = tmp_path / "credentials" / "instagram.cookies.txt"
    previous = target.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic storage failure")

    with monkeypatch.context() as patch:
        patch.setattr("vela.source_sessions.os.replace", fail)
        with pytest.raises(SourceSessionError):
            store.import_text("instagram", HEADER + row(value="synthetic-new-session"))
    assert target.read_bytes() == previous
    assert [path.name for path in target.parent.iterdir()] == ["instagram.cookies.txt"]


@pytest.mark.parametrize("url,site", [
    ("https://www.instagram.com/reel/synthetic/", "instagram"),
    ("https://instagr.am/synthetic", "instagram"),
    ("https://youtu.be/synthetic", "youtube"),
    ("https://m.youtube.com/watch?v=synthetic", "youtube"),
    ("https://fb.watch/synthetic", "facebook"),
    ("https://x.com/synthetic", "x"),
    ("https://twitter.com/synthetic", "x"),
    ("https://mail.google.com/synthetic", None),
    ("https://google.com/synthetic", None),
    ("https://instagram.com.unrelated.invalid/synthetic", None),
    ("https://notinstagram.com/synthetic", None),
    ("https://synthetic-user@instagram.com/synthetic", None),
    ("file:///instagram.com", None),
])
def test_source_selection_uses_real_host_boundaries(url, site):
    assert site_for_url(url) == site


@pytest.mark.skipif(os.name != "posix", reason="Production symlink and mode guards use Linux openat")
def test_symlink_store_cookie_and_hardlink_are_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(SourceSessionError):
        SourceSessionStore(linked).import_text("instagram", HEADER + row())
    original = tmp_path / "private.txt"
    original_text = HEADER + row()
    original.write_text(original_text)
    cookie = real / "instagram.cookies.txt"
    cookie.symlink_to(original)
    store = SourceSessionStore(real)
    assert store.status("instagram")["state"] == "invalid"
    with pytest.raises(SourceSessionError):
        store.import_text("instagram", HEADER + row())
    assert original.read_text() == original_text
    cookie.unlink()
    os.link(original, cookie)
    with pytest.raises(SourceSessionError):
        store.import_text("instagram", HEADER + row())


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")
    settings = Settings(state_dir=tmp_path / "state", credentials_dir=tmp_path / "credentials", base_url="http://testserver")
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, settings


def test_api_status_import_delete_never_disclose_cookie_material(api):
    client, settings = api
    assert len(client.get("/api/source-sessions").json()["items"]) == 5
    text = HEADER + row() + row(name="ds_user_id", value="synthetic-account-identity")
    response = client.put("/api/source-sessions/instagram", content=text, headers={"Origin": "http://testserver", "Content-Type": "text/plain"})
    assert response.status_code == 200 and response.json()["cookie_count"] == 2
    assert "synthetic" not in response.text
    assert "synthetic" not in client.get("/api/source-sessions").text
    database_bytes = b"".join(path.read_bytes() for path in settings.state_dir.iterdir() if path.is_file())
    assert b"synthetic-session-only" not in database_bytes and b"synthetic-account-identity" not in database_bytes
    assert client.delete("/api/source-sessions/instagram", headers={"Origin": "http://testserver"}).json() == {"ok": True}
    assert not (settings.credentials_dir / "instagram.cookies.txt").exists()


def test_api_limits_content_type_origin_host_and_body(api):
    client, _ = api
    path = "/api/source-sessions/instagram"
    headers = {"Origin": "http://testserver", "Content-Type": "text/plain"}
    assert client.put(path, content=HEADER + row()).status_code == 403
    assert client.put(path, content=HEADER + row(), headers={**headers, "Origin": "http://unrelated.invalid"}).status_code == 403
    assert client.delete(path).status_code == 403
    assert client.put(path, content=HEADER + row(), headers={**headers, "Host": "unrelated.invalid"}).status_code == 400
    assert client.put(path, json={}, headers={"Origin": "http://testserver"}).status_code == 415
    assert client.put(path, content=b"\xff", headers=headers).status_code == 422
    assert client.put(path, content=b"x"*(MAX_COOKIE_BYTES+1), headers=headers).status_code == 413
    assert client.put("/api/source-sessions/unknown", content=HEADER + row(), headers=headers).status_code == 404
    assert client.delete("/api/source-sessions/unknown", headers=headers).status_code == 404


def test_chunked_upload_cannot_bypass_size_limit(api):
    client, _ = api
    chunks = iter([b"x"*MAX_COOKIE_BYTES, b"x"])
    response = client.put("/api/source-sessions/instagram", content=chunks, headers={"Origin": "http://testserver", "Content-Type": "text/plain"})
    assert response.status_code == 413
