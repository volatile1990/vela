import asyncio
from pathlib import Path
import sys
import json
import os
import signal
import subprocess
import shutil

import pytest

from vela.engine import Engine, EngineError, EngineCancelled, format_selector, public_metadata, validate_source_url, recover_process, _process_identity, _group_running, METADATA_TEMPLATE, PROGRESS_TEMPLATE


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a", "http://192.168.1.2/", "https://[::1]/", "http://169.254.169.254/", "https://localhost/a", "https://nas.home.arpa/", "https://user:pass@example.com/", "https://example.com:2222/a"])
def test_engine_rejects_nonpublic_input(url):
    with pytest.raises(EngineError):
        validate_source_url(url)


def test_metadata_does_not_leak_extractor_fields():
    result = public_metadata({"id": "a", "title": "Video", "cookies": "secret", "http_headers": {"Authorization": "token"}, "formats": [{"url": "https://cdn/?secret=1"}]})
    assert "secret" not in str(result)
    assert result["title"] == "Video"
    assert "height<=?720" in format_selector("compatible", 720)
    assert "avc1" in format_selector("compatible", 720)


def test_pipes_are_drained_concurrently(tmp_path):
    script = tmp_path / "pipes.py"
    script.write_text("import sys\nfor i in range(10000):\n sys.stderr.write('x' * 80 + '\\n')\nprint('done')\n", encoding="utf-8")
    found = []

    async def receive(line):
        found.append(line)

    asyncio.run(Engine()._run([sys.executable, str(script)], tmp_path, receive, None, 10))
    assert found == ["done"]
    assert not (tmp_path / ".vela-process.json").exists()


def test_output_limit_stops_process(tmp_path):
    script = tmp_path / "large.py"
    script.write_text("print('x' * 10000)\n", encoding="utf-8")

    async def receive(_):
        pass

    with pytest.raises(EngineError) as error:
        asyncio.run(Engine(max_line=1024)._run([sys.executable, str(script)], tmp_path, receive, None, 5))
    assert error.value.category == "output_limit"


def test_cancel_and_timeout_terminate_engine(tmp_path):
    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    async def receive(_):
        pass

    async def cancel_run():
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.2, cancel.set)
        await Engine(cancel_grace=0.1)._run([sys.executable, str(script)], tmp_path, receive, cancel, 5)

    with pytest.raises(EngineCancelled):
        asyncio.run(cancel_run())
    with pytest.raises(EngineError) as error:
        asyncio.run(Engine(cancel_grace=0.1)._run([sys.executable, str(script)], tmp_path, receive, None, 0.2))
    assert error.value.category == "timeout"


def test_dns_to_private_address_is_rejected(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.20.1", 0))])
    with pytest.raises(EngineError) as error:
        asyncio.run(Engine().validate_dns("https://public-looking.example/path"))
    assert error.value.category == "validation"


@pytest.mark.skipif(os.name != "posix", reason="Linux production process groups")
def test_recovery_kills_only_recorded_process_identity(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    marker = tmp_path / ".vela-process.json"
    try:
        identity = _process_identity(proc.pid)
        assert identity is not None
        marker.write_text(json.dumps({"pid": proc.pid, "identity": "different-starttime"}))
        recover_process(tmp_path)
        assert proc.poll() is None  # PID reuse must never kill an unrelated process.
        marker.write_text(json.dumps({"pid": proc.pid, "identity": identity}))
        recover_process(tmp_path)
        assert proc.wait(timeout=5) == -signal.SIGKILL
        assert not marker.exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


@pytest.mark.skipif(os.name != "posix", reason="Linux production process groups")
def test_cancellation_also_kills_stubborn_grandchild(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text("import subprocess,sys,time\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\nPath('child.pid').write_text(str(child.pid))\ntime.sleep(60)\n")

    async def receive(_):
        pass

    async def run():
        cancel = asyncio.Event()
        task = asyncio.create_task(Engine(cancel_grace=0.1)._run([sys.executable, str(script)], tmp_path, receive, cancel, 10))
        while not (tmp_path / "child.pid").exists():
            await asyncio.sleep(0.01)
        group = json.loads((tmp_path / ".vela-process.json").read_text())["pid"]
        await asyncio.sleep(0.1)
        cancel.set()
        with pytest.raises(EngineCancelled):
            await task
        for _ in range(50):
            if not _group_running(group):
                break
            await asyncio.sleep(0.01)
        assert not _group_running(group)

    asyncio.run(run())


def test_real_ytdlp_missing_optional_metadata_is_valid_json():
    yt_dlp = pytest.importorskip("yt_dlp")
    info = {"id": "flower", "title": 'A "quoted" title\nnext line', "extractor_key": "Generic"}
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        rendered = ydl.evaluate_outtmpl(METADATA_TEMPLATE, info)
    metadata = json.loads(rendered)
    assert metadata["title"] == info["title"]
    assert metadata["id"] == "flower"
    assert public_metadata(metadata)["duration"] is None


def test_real_ytdlp_progress_template_excludes_private_info_and_missing_values():
    yt_dlp = pytest.importorskip("yt_dlp")
    info = {"progress": {"status": "downloading", "downloaded_bytes": 1024,
        "speed": None, "info_dict": {"url": "https://cdn.example/?token=private"}}}
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        rendered = ydl.evaluate_outtmpl(PROGRESS_TEMPLATE, info)
    progress = json.loads(rendered)
    assert progress == {"status": "downloading", "downloaded_bytes": 1024}
    assert "private" not in rendered


def test_real_ytdlp_direct_media_with_unknown_height_remains_selectable():
    yt_dlp = pytest.importorskip("yt_dlp")
    info = {"id": "flower", "title": "flower", "formats": [
        {"format_id": "mp4", "url": "https://cdn.example/flower.mp4", "ext": "mp4"}]}
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        selector = ydl.build_format_selector(format_selector("original", 1080))
        chosen = list(selector({"formats": info["formats"], "has_merged_format": False, "incomplete_formats": False}))
    assert chosen and chosen[0]["format_id"] == "mp4"
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        selector = ydl.build_format_selector(format_selector("compatible", 1080))
        chosen = list(selector({"formats": info["formats"], "has_merged_format": False, "incomplete_formats": False}))
    assert chosen and chosen[0]["format_id"] == "mp4"


@pytest.mark.parametrize("streams,valid", [
    ([{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "aac"}], True),
    ([{"codec_type": "video", "codec_name": "h264"}], True),
    ([{"codec_type": "video", "codec_name": "av1"}, {"codec_type": "audio", "codec_name": "aac"}], False),
    ([{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "opus"}], False),
])
def test_compatible_verifies_real_codecs_instead_of_trusting_container(tmp_path, streams, valid):
    class ProbeEngine(Engine):
        async def _run(self, args, work_dir, receive, cancel, timeout):
            await receive(json.dumps({"streams": streams}))
    result = ProbeEngine().verify_compatible(tmp_path / "media.mp4", tmp_path)
    if valid:
        asyncio.run(result)
    else:
        with pytest.raises(EngineError) as error:
            asyncio.run(result)
        assert error.value.category == "format"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Engine image supplies FFmpeg and ffprobe")
def test_real_ffprobe_accepts_h264_and_rejects_other_video_codecs(tmp_path):
    compatible = tmp_path / "h264.mp4"
    incompatible = tmp_path / "ffv1.mkv"
    for target, codec in ((compatible, "libx264"), (incompatible, "ffv1")):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1",
                        "-c:v", codec, "-threads", "1", str(target)], check=True, timeout=20)
    asyncio.run(Engine().verify_compatible(compatible, tmp_path))
    with pytest.raises(EngineError) as error:
        asyncio.run(Engine().verify_compatible(incompatible, tmp_path))
    assert error.value.category == "format"
