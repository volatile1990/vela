"""Synthetic sessions only: never read browser profiles or production jars."""
import asyncio
import hashlib
import os
from pathlib import Path
import stat
import sys
from http.cookiejar import MozillaCookieJar
from urllib.request import Request

import pytest

from vela.engine import Engine, EngineError, EngineCancelled


HEADER = "# Netscape HTTP Cookie File\n"


def jar_line(domain: str, name: str = "sessionid", value: str = "synthetic-session") -> str:
    return f".{domain}\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}\n"


def write_jar(root: Path, site: str, content: str) -> Path:
    root.mkdir(exist_ok=True)
    target = root / f"{site}.cookies.txt"
    target.write_text(HEADER + content, encoding="utf-8")
    target.chmod(0o600)
    return target


@pytest.mark.parametrize("url,site", [
    ("https://www.instagram.com/reel/synthetic/", "instagram"),
    ("https://instagr.am/p/synthetic/", "instagram"),
    ("https://www.youtube.com/watch?v=synthetic", "youtube"),
    ("https://youtu.be/synthetic", "youtube"),
    ("https://vm.tiktok.com/synthetic/", "tiktok"),
    ("https://www.facebook.com/watch/?v=synthetic", "facebook"),
    ("https://fb.watch/synthetic/", "facebook"),
    ("https://x.com/synthetic/status/1", "x"),
    ("https://twitter.com/synthetic/status/1", "x"),
])
def test_fixed_site_and_shortlink_mapping(url, site):
    from vela.source_sessions import site_for_url
    assert site_for_url(url) == site


@pytest.mark.parametrize("host", ["evilinstagram.com", "instagram.com.evil.example", "youtube.com.evil.example", "notyoutube.com", "x.com.evil.example", "nottwitter.com", "fb.watch.evil.example", "example.com"])
def test_host_end_boundary_never_selects_unrelated_sessions(tmp_path, host):
    from vela.source_sessions import site_for_url
    url = f"https://{host}/synthetic"
    assert site_for_url(url) is None
    credentials = tmp_path / "credentials"
    write_jar(credentials, "instagram", jar_line("instagram.com"))
    with Engine(credentials_dir=credentials)._session_args(url, tmp_path / "work") as args:
        assert args == []


def test_only_matching_jar_is_copied_private_and_source_is_unchanged(tmp_path):
    credentials = tmp_path / "credentials"
    source = write_jar(credentials, "instagram", jar_line("instagram.com", value="synthetic-instagram"))
    write_jar(credentials, "youtube", jar_line("youtube.com", "SID", "synthetic-youtube"))
    source.chmod(0o400)  # The worker's original volume is mounted read-only.
    original = source.read_bytes()
    work = tmp_path / "work"
    try:
        with Engine(credentials_dir=credentials)._session_args("https://www.instagram.com/reel/synthetic/", work) as args:
            assert args == ["--cookies", str(work / ".vela-cookies.txt")]
            copy = Path(args[1])
            assert b"synthetic-instagram" in copy.read_bytes()
            assert b"synthetic-youtube" not in copy.read_bytes()
            if os.name != "nt":
                assert stat.S_IMODE(copy.stat().st_mode) == 0o600
            copy.write_text(copy.read_text() + "# simulated downloader update\n")
        assert source.read_bytes() == original
        assert not (work / ".vela-cookies.txt").exists()
    finally:
        source.chmod(0o600)


def test_missing_matching_jar_remains_anonymous_and_clears_crash_remnant(tmp_path):
    credentials = tmp_path / "credentials"
    write_jar(credentials, "youtube", jar_line("youtube.com", "SID", "synthetic-youtube"))
    work = tmp_path / "work"
    work.mkdir()
    (work / ".vela-cookies.txt").write_text("synthetic-crash-remnant")
    with Engine(credentials_dir=credentials)._session_args("https://www.instagram.com/reel/synthetic/", work) as args:
        assert args == []
        assert not (work / ".vela-cookies.txt").exists()


def test_jar_revalidation_rejects_foreign_only_and_filters_mixed_domains(tmp_path):
    credentials = tmp_path / "credentials"
    source = write_jar(credentials, "instagram", jar_line("facebook.com", value="synthetic-facebook"))
    work = tmp_path / "work"
    engine = Engine(credentials_dir=credentials)
    with pytest.raises(EngineError) as error:
        with engine._session_args("https://www.instagram.com/reel/synthetic/", work):
            pass
    assert error.value.category == "session_invalid"
    assert not (work / ".vela-cookies.txt").exists()
    source.write_text(HEADER + jar_line("instagram.com", value="synthetic-instagram") + jar_line("facebook.com", value="synthetic-facebook"))
    with engine._session_args("https://www.instagram.com/reel/synthetic/", work) as args:
        copied = Path(args[1]).read_text()
        assert "synthetic-instagram" in copied
        assert "facebook" not in copied


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "cancel", "task_cancel"])
def test_real_subprocess_cleans_job_copy_on_all_outcomes(tmp_path, outcome):
    credentials = tmp_path / "credentials"
    source = write_jar(credentials, "instagram", jar_line("instagram.com"))
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    work = tmp_path / "work"
    script = tmp_path / "runner.py"
    if outcome == "success":
        script.write_text("pass\n")
    elif outcome == "error":
        script.write_text("import sys\nsys.stderr.write('ERROR: Login required\\n')\nsys.exit(1)\n")
    else:
        script.write_text("import time\ntime.sleep(60)\n")

    async def receive(_):
        pass

    async def run():
        engine = Engine(credentials_dir=credentials, cancel_grace=0.1)
        cancel = asyncio.Event()
        timeout = 0.2 if outcome == "timeout" else 10
        if outcome == "cancel":
            asyncio.get_running_loop().call_later(0.2, cancel.set)
        task = asyncio.create_task(engine._run_source([sys.executable, str(script), "--", "https://www.instagram.com/reel/synthetic/"],
            "https://www.instagram.com/reel/synthetic/", work, receive, cancel, timeout))
        if outcome == "task_cancel":
            asyncio.get_running_loop().call_later(0.2, task.cancel)
        await task

    if outcome == "success":
        asyncio.run(run())
    elif outcome == "task_cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
    else:
        with pytest.raises(EngineError) as error:
            asyncio.run(run())
        assert error.value.category == {"error": "login_required", "timeout": "timeout", "cancel": "cancelled"}[outcome]
        assert "synthetic-session" not in str(error.value)
        assert str(source) not in str(error.value)
        assert str(work) not in str(error.value)
    assert not (work / ".vela-cookies.txt").exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


def test_parallel_jobs_have_independent_session_copies(tmp_path):
    credentials = tmp_path / "credentials"
    write_jar(credentials, "instagram", jar_line("instagram.com"))
    engine = Engine(credentials_dir=credentials)
    with engine._session_args("https://www.instagram.com/reel/synthetic/", tmp_path / "job-a") as first:
        with engine._session_args("https://www.instagram.com/reel/synthetic/", tmp_path / "job-b") as second:
            assert first[1] != second[1]
            assert Path(first[1]).exists() and Path(second[1]).exists()
        assert Path(first[1]).exists() and not Path(second[1]).exists()
    assert not Path(first[1]).exists()


def test_native_cookie_jar_keeps_redirects_scoped_to_cookie_domains(tmp_path):
    credentials = tmp_path / "credentials"
    write_jar(credentials, "instagram", jar_line("instagram.com"))
    engine = Engine(credentials_dir=credentials)
    with engine._session_args("https://instagr.am/p/synthetic/", tmp_path / "work") as args:
        jar = MozillaCookieJar(args[1])
        jar.load(ignore_discard=True, ignore_expires=False)
        for url, expected in [
            ("https://www.instagram.com/synthetic/", True),
            ("https://instagram.com.evil.example/synthetic/", False),
            ("https://evilinstagram.com/synthetic/", False),
            ("https://www.facebook.com/synthetic/", False),
            ("http://www.instagram.com/synthetic/", False),
        ]:
            request = Request(url)
            jar.add_cookie_header(request)
            assert bool(request.get_header("Cookie")) is expected


def test_download_metadata_records_only_safe_session_selection(tmp_path):
    credentials = tmp_path / "credentials"
    write_jar(credentials, "instagram", jar_line("instagram.com"))
    work = tmp_path / "work"
    events = []

    class StructuredEngine(Engine):
        async def validate_dns(self, url):
            pass

        async def _run(self, args, work_dir, receive, cancel, timeout):
            assert "--cookies" in args
            assert str(credentials) not in " ".join(args)
            file = work_dir / "media.mp4"
            file.write_bytes(b"synthetic media")
            await receive('VELA_META:{"id":"synthetic","title":"Synthetic"}')
            import json
            await receive("VELA_FILE:" + json.dumps(str(file)))

    async def event(kind, payload):
        events.append((kind, payload))

    result = asyncio.run(StructuredEngine(credentials_dir=credentials).download(
        "https://www.instagram.com/reel/synthetic/", work, on_event=event))
    assert result.metadata["source_session_used"] is True
    assert result.metadata["source_session_site"] == "instagram"
    public = str(result.metadata) + str(events)
    assert "synthetic-session" not in public
    assert ".cookies.txt" not in public
    assert str(credentials) not in public
    assert not (work / ".vela-cookies.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="Linux special-file handling")
def test_special_session_file_is_rejected_without_blocking(tmp_path):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    os.mkfifo(credentials / "instagram.cookies.txt", 0o600)
    with pytest.raises(EngineError) as error:
        with Engine(credentials_dir=credentials)._session_args("https://www.instagram.com/reel/synthetic/", tmp_path / "work"):
            pass
    assert error.value.category == "session_invalid"


def test_oversized_session_is_bounded_and_errors_do_not_reveal_its_path(tmp_path):
    credentials = tmp_path / "credentials"
    source = write_jar(credentials, "instagram", "x" * (256 * 1024 + 1))
    with pytest.raises(EngineError) as error:
        with Engine(credentials_dir=credentials)._session_args("https://www.instagram.com/reel/synthetic/", tmp_path / "work"):
            pass
    assert error.value.category == "session_invalid"
    assert str(source) not in str(error.value)
    assert not (tmp_path / "work" / ".vela-cookies.txt").exists()
